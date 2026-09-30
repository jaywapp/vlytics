using System.Text;
using Vlytics.Settings.Core;

namespace Vlytics.Settings.Tests;

public sealed class SecretVaultTests
{
    [Fact]
    public void SaveRoundTripsEncryptedValueWithoutPlaintextOnDisk()
    {
        using var directory = new TestDirectory();
        var vault = new SecretVault(directory.Path);
        const string name = "VLYTICS_OPENAI_API_KEY";
        const string secret = "test-plaintext-value-7c29e64f";

        vault.Save(name, secret);

        Assert.True(vault.Contains(name));
        Assert.Equal(secret, vault.Read(name));
        var files = Directory.GetFiles(directory.Path, "*", SearchOption.AllDirectories);
        Assert.Single(files);
        Assert.DoesNotContain(files, path => path.Contains(name, StringComparison.Ordinal));
        Assert.All(files, path => Assert.DoesNotContain(secret, Encoding.UTF8.GetString(File.ReadAllBytes(path))));
    }

    [Fact]
    public void DeleteRemovesSecret()
    {
        using var directory = new TestDirectory();
        var vault = new SecretVault(directory.Path);
        const string name = "VLYTICS_ANTHROPIC_API_KEY";
        vault.Save(name, "temporary-value");

        vault.Delete(name);

        Assert.False(vault.Contains(name));
        Assert.Null(vault.Read(name));
    }

    [Fact]
    public void InitializeDefaultsGeneratesStableDatabaseAndAuthSecrets()
    {
        using var directory = new TestDirectory();
        var vault = new SecretVault(directory.Path);

        vault.InitializeDefaults();
        var first = vault.GetEnvironment();
        vault.InitializeDefaults();
        var second = vault.GetEnvironment();

        Assert.Equal(7, first.Count);
        Assert.Equal(first, second);
        Assert.DoesNotContain("VLYTICS_OPENAI_API_KEY", first.Keys);
        Assert.All(first.Values, value => Assert.True(value.Length >= 40));
    }

    [Fact]
    public void RejectsNamesOutsideAllowlistWithoutEchoingName()
    {
        using var directory = new TestDirectory();
        var vault = new SecretVault(directory.Path);
        const string disallowedName = "VLYTICS_UNEXPECTED_SECRET";

        var exception = Assert.Throws<ArgumentException>(() => vault.Save(disallowedName, "value"));

        Assert.DoesNotContain(disallowedName, exception.Message, StringComparison.Ordinal);
    }
}
