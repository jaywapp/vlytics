using System.Text.Json;

namespace Vlytics.Settings.Core;

public sealed class SettingsStore
{
    private static readonly HashSet<string> Providers = new(StringComparer.Ordinal)
    {
        "openai",
        "anthropic",
        "google",
    };

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        WriteIndented = true,
    };

    public SettingsStore(string dataDirectory)
    {
        DataDirectory = SecureDirectory.CreateForCurrentUser(dataDirectory);
        SettingsPath = Path.Combine(DataDirectory, "settings.json");
    }

    public string DataDirectory { get; }

    public string SettingsPath { get; }

    public AppSettings Load()
    {
        if (!File.Exists(SettingsPath))
        {
            return new AppSettings();
        }

        AppSettings settings;
        try
        {
            using var stream = new FileStream(
                SettingsPath,
                FileMode.Open,
                FileAccess.Read,
                FileShare.Read,
                bufferSize: 4096,
                FileOptions.SequentialScan);
            settings = JsonSerializer.Deserialize<AppSettings>(stream, JsonOptions)
                ?? throw new InvalidDataException("The settings file is empty.");
        }
        catch (JsonException exception)
        {
            throw new InvalidDataException("The settings file is not valid JSON.", exception);
        }

        ValidateAndNormalize(settings);
        return settings;
    }

    public void Save(AppSettings settings)
    {
        ArgumentNullException.ThrowIfNull(settings);
        ValidateAndNormalize(settings);

        var temporaryPath = Path.Combine(DataDirectory, $"settings.{Guid.NewGuid():N}.tmp");
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
                JsonSerializer.Serialize(stream, settings, JsonOptions);
                stream.Flush(flushToDisk: true);
            }

            File.Move(temporaryPath, SettingsPath, overwrite: true);
        }
        finally
        {
            TryDelete(temporaryPath);
        }
    }

    private static void ValidateAndNormalize(AppSettings settings)
    {
        if (string.IsNullOrWhiteSpace(settings.RuntimeRoot)
            || !Path.IsPathFullyQualified(settings.RuntimeRoot)
            || settings.RuntimeRoot.IndexOfAny(Path.GetInvalidPathChars()) >= 0)
        {
            throw new ArgumentException("RuntimeRoot must be a valid absolute path.", nameof(settings));
        }

        if (!Providers.Contains(settings.Provider))
        {
            throw new ArgumentException("Provider must be openai, anthropic, or google.", nameof(settings));
        }

        ValidateNonNegative(settings.PricingToBudgetRate, nameof(settings.PricingToBudgetRate));
        ValidateNonNegative(settings.InputPrice, nameof(settings.InputPrice));
        ValidateNonNegative(settings.OutputPrice, nameof(settings.OutputPrice));
        ValidateNonNegative(settings.DailyBudget, nameof(settings.DailyBudget));
        ValidateNonNegative(settings.MonthlyBudget, nameof(settings.MonthlyBudget));
        ValidatePositive(settings.MaxCallsPerMatch, nameof(settings.MaxCallsPerMatch));
        ValidatePositive(settings.DailyCallLimit, nameof(settings.DailyCallLimit));
        ValidatePositive(settings.MonthlyCallLimit, nameof(settings.MonthlyCallLimit));
        ValidatePositive(settings.MaxInputTokens, nameof(settings.MaxInputTokens));
        ValidatePositive(settings.MaxOutputTokens, nameof(settings.MaxOutputTokens));
        ValidatePort(settings.WebPort, nameof(settings.WebPort), minimum: 1024);
        ValidatePort(settings.SshPort, nameof(settings.SshPort));

        settings.ModelId ??= string.Empty;
        settings.PinnedModelVersion ??= string.Empty;
        settings.DryRunEvidencePath ??= string.Empty;
        settings.SshHost ??= string.Empty;
        settings.SshUser ??= string.Empty;
        settings.Images ??= new Dictionary<string, string>(StringComparer.Ordinal);

        var unknownImage = settings.Images.Keys.FirstOrDefault(
            key => !AppSettings.ImageNames.Contains(key, StringComparer.Ordinal));
        if (unknownImage is not null)
        {
            throw new ArgumentException("Images contains an unsupported key.", nameof(settings));
        }

        foreach (var name in AppSettings.ImageNames)
        {
            settings.Images.TryAdd(name, string.Empty);
            settings.Images[name] ??= string.Empty;
        }
    }

    private static void ValidateNonNegative(decimal value, string name)
    {
        if (value < 0)
        {
            throw new ArgumentOutOfRangeException(name, "The value cannot be negative.");
        }
    }

    private static void ValidatePositive(int value, string name)
    {
        if (value <= 0)
        {
            throw new ArgumentOutOfRangeException(name, "The value must be positive.");
        }
    }

    private static void ValidatePort(int value, string name, int minimum = 1)
    {
        if (value < minimum || value > 65535)
        {
            throw new ArgumentOutOfRangeException(name, "The value must be a valid TCP port.");
        }
    }

    private static void TryDelete(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch (IOException)
        {
        }
        catch (UnauthorizedAccessException)
        {
        }
    }
}
