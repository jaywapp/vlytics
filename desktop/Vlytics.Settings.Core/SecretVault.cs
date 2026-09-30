using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

namespace Vlytics.Settings.Core;

public sealed class SecretVault
{
    private const int CryptProtectUiForbidden = 0x1;
    private static readonly byte[] OptionalEntropy = Encoding.UTF8.GetBytes("Vlytics.Settings.Core.v1");
    private static readonly HashSet<string> AllowedNameSet = new(StringComparer.Ordinal)
    {
        "VLYTICS_OPENAI_API_KEY",
        "VLYTICS_ANTHROPIC_API_KEY",
        "VLYTICS_GOOGLE_API_KEY",
        "VLYTICS_OPERATOR_AUTH_SECRET",
        "VLYTICS_READONLY_AUTH_SECRET",
        "MIGRATOR_DATABASE_PASSWORD",
        "COLLECTOR_DATABASE_PASSWORD",
        "ENGINE_DATABASE_PASSWORD",
        "MARKET_INGEST_DATABASE_PASSWORD",
        "READ_API_DATABASE_PASSWORD",
    };

    private static readonly string[] GeneratedNames =
    [
        "VLYTICS_OPERATOR_AUTH_SECRET",
        "VLYTICS_READONLY_AUTH_SECRET",
        "MIGRATOR_DATABASE_PASSWORD",
        "COLLECTOR_DATABASE_PASSWORD",
        "ENGINE_DATABASE_PASSWORD",
        "MARKET_INGEST_DATABASE_PASSWORD",
        "READ_API_DATABASE_PASSWORD",
    ];

    private readonly object sync = new();
    private readonly string secretsDirectory;

    public SecretVault(string dataDirectory)
    {
        var protectedDataDirectory = SecureDirectory.CreateForCurrentUser(dataDirectory);
        secretsDirectory = SecureDirectory.CreateForCurrentUser(Path.Combine(protectedDataDirectory, "secrets"));
    }

    internal static IEnumerable<string> AllowedNames => AllowedNameSet;

    public void Save(string name, string secret)
    {
        ValidateName(name);
        ArgumentException.ThrowIfNullOrEmpty(secret);

        var plaintext = Encoding.UTF8.GetBytes(secret);
        try
        {
            var ciphertext = Protect(plaintext);
            WriteCiphertext(GetSecretPath(name), ciphertext);
            CryptographicOperations.ZeroMemory(ciphertext);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(plaintext);
        }
    }

    public string? Read(string name)
    {
        ValidateName(name);
        var path = GetSecretPath(name);
        if (!File.Exists(path))
        {
            return null;
        }

        byte[] ciphertext;
        try
        {
            ciphertext = File.ReadAllBytes(path);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            throw new InvalidOperationException("The encrypted secret could not be read.", exception);
        }

        try
        {
            var plaintext = Unprotect(ciphertext);
            try
            {
                return Encoding.UTF8.GetString(plaintext);
            }
            finally
            {
                CryptographicOperations.ZeroMemory(plaintext);
            }
        }
        finally
        {
            CryptographicOperations.ZeroMemory(ciphertext);
        }
    }

    public bool Contains(string name)
    {
        ValidateName(name);
        return File.Exists(GetSecretPath(name));
    }

    public void Delete(string name)
    {
        ValidateName(name);
        File.Delete(GetSecretPath(name));
    }

    public void InitializeDefaults()
    {
        lock (sync)
        {
            foreach (var name in GeneratedNames)
            {
                if (!Contains(name))
                {
                    Save(name, GenerateSecret());
                }
            }
        }
    }

    public Dictionary<string, string> GetEnvironment()
    {
        var environment = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var name in AllowedNameSet.Order(StringComparer.Ordinal))
        {
            var value = Read(name);
            if (value is not null)
            {
                environment.Add(name, value);
            }
        }

        return environment;
    }

    private static string GenerateSecret()
    {
        Span<byte> bytes = stackalloc byte[32];
        RandomNumberGenerator.Fill(bytes);
        return Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');
    }

    private string GetSecretPath(string name)
    {
        var nameBytes = Encoding.UTF8.GetBytes(name);
        var hash = SHA256.HashData(nameBytes);
        return Path.Combine(secretsDirectory, $"{Convert.ToHexString(hash)}.bin");
    }

    private static void ValidateName(string name)
    {
        if (name is null || !AllowedNameSet.Contains(name))
        {
            throw new ArgumentException("Secret name is not allowed.", nameof(name));
        }
    }

    private static void WriteCiphertext(string destinationPath, byte[] ciphertext)
    {
        var directory = Path.GetDirectoryName(destinationPath)
            ?? throw new InvalidOperationException("The secret storage path is invalid.");
        var temporaryPath = Path.Combine(directory, $"secret.{Guid.NewGuid():N}.tmp");

        try
        {
            using (var stream = new FileStream(
                temporaryPath,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None,
                bufferSize: 4096,
                FileOptions.WriteThrough))
            {
                stream.Write(ciphertext);
                stream.Flush(flushToDisk: true);
            }

            File.Move(temporaryPath, destinationPath, overwrite: true);
        }
        finally
        {
            try
            {
                File.Delete(temporaryPath);
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
        }
    }

    private static byte[] Protect(byte[] plaintext)
    {
        return Transform(plaintext, protect: true);
    }

    private static byte[] Unprotect(byte[] ciphertext)
    {
        return Transform(ciphertext, protect: false);
    }

    private static byte[] Transform(byte[] input, bool protect)
    {
        var inputBlob = DataBlob.FromBytes(input);
        var entropyBlob = DataBlob.FromBytes(OptionalEntropy);
        DataBlob outputBlob = default;
        try
        {
            var succeeded = protect
                ? CryptProtectData(
                    ref inputBlob,
                    "Vlytics Settings",
                    ref entropyBlob,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    CryptProtectUiForbidden,
                    out outputBlob)
                : CryptUnprotectData(
                    ref inputBlob,
                    IntPtr.Zero,
                    ref entropyBlob,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    CryptProtectUiForbidden,
                    out outputBlob);

            if (!succeeded)
            {
                var errorCode = Marshal.GetLastWin32Error();
                throw new CryptographicException(
                    protect ? "Secret encryption failed." : "Secret decryption failed.",
                    new Win32Exception(errorCode));
            }

            try
            {
                var output = new byte[outputBlob.Length];
                Marshal.Copy(outputBlob.Data, output, 0, output.Length);
                return output;
            }
            finally
            {
                if (outputBlob.Data != IntPtr.Zero)
                {
                    LocalFree(outputBlob.Data);
                }
            }
        }
        finally
        {
            inputBlob.Dispose();
            entropyBlob.Dispose();
        }
    }

    [DllImport("Crypt32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CryptProtectData(
        ref DataBlob dataIn,
        string dataDescription,
        ref DataBlob optionalEntropy,
        IntPtr reserved,
        IntPtr promptStruct,
        int flags,
        out DataBlob dataOut);

    [DllImport("Crypt32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CryptUnprotectData(
        ref DataBlob dataIn,
        IntPtr dataDescription,
        ref DataBlob optionalEntropy,
        IntPtr reserved,
        IntPtr promptStruct,
        int flags,
        out DataBlob dataOut);

    [DllImport("Kernel32.dll", SetLastError = true)]
    private static extern IntPtr LocalFree(IntPtr memory);

    [StructLayout(LayoutKind.Sequential)]
    private struct DataBlob : IDisposable
    {
        public int Length;
        public IntPtr Data;

        public static DataBlob FromBytes(byte[] bytes)
        {
            var blob = new DataBlob
            {
                Length = bytes.Length,
                Data = Marshal.AllocHGlobal(bytes.Length),
            };
            Marshal.Copy(bytes, 0, blob.Data, bytes.Length);
            return blob;
        }

        public void Dispose()
        {
            if (Data != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(Data);
                Data = IntPtr.Zero;
                Length = 0;
            }
        }
    }
}
