using System.Text.Json;
using Vlytics.Settings.Core;

namespace Vlytics.Settings.Tests;

public sealed class SettingsStoreTests
{
    [Fact]
    public void DefaultsMatchDesktopContract()
    {
        var settings = new AppSettings();

        Assert.Equal("openai", settings.Provider);
        Assert.Equal("gpt-6-sol", settings.ModelId);
        Assert.Equal(8080, settings.WebPort);
        Assert.Equal(22, settings.SshPort);
        Assert.Equal(2.50m, settings.InputPrice);
        Assert.Equal(10m, settings.OutputPrice);
        Assert.Equal(7, settings.Images.Count);
        Assert.All(settings.Images.Values, Assert.Empty);
    }

    [Fact]
    public void SaveAndLoadRoundTripsSettingsWithoutSecretFields()
    {
        using var directory = new TestDirectory();
        var store = new SettingsStore(directory.Path);
        var settings = new AppSettings
        {
            RuntimeRoot = directory.GetPath("runtime"),
            Provider = "anthropic",
            ModelId = "claude-test",
            PinnedModelVersion = string.Empty,
            DryRunEvidencePath = string.Empty,
            SshHost = string.Empty,
            SshUser = string.Empty,
        };
        settings.Images["VLYTICS_BACKEND_IMAGE"] = "registry.example/vlytics:1";

        store.Save(settings);
        var loaded = store.Load();

        Assert.Equal(settings.RuntimeRoot, loaded.RuntimeRoot);
        Assert.Equal(settings.Provider, loaded.Provider);
        Assert.Equal(settings.ModelId, loaded.ModelId);
        Assert.Equal(settings.WebPort, loaded.WebPort);
        Assert.Equal(settings.Images, loaded.Images);
        using var json = JsonDocument.Parse(File.ReadAllText(store.SettingsPath));
        Assert.DoesNotContain(
            json.RootElement.EnumerateObject(),
            property => property.Name.Contains("secret", StringComparison.OrdinalIgnoreCase)
                || property.Name.Contains("api_key", StringComparison.OrdinalIgnoreCase)
                || property.Name.Contains("password", StringComparison.OrdinalIgnoreCase));
    }

    [Fact]
    public void SaveAllowsEmptyOperationalFields()
    {
        using var directory = new TestDirectory();
        var store = new SettingsStore(directory.Path);
        var settings = new AppSettings
        {
            RuntimeRoot = directory.GetPath("runtime"),
            PinnedModelVersion = string.Empty,
            DryRunEvidencePath = string.Empty,
            SshHost = string.Empty,
            SshUser = string.Empty,
        };

        store.Save(settings);

        Assert.True(File.Exists(store.SettingsPath));
    }

    [Theory]
    [InlineData("OPENAI")]
    [InlineData("unknown")]
    [InlineData("")]
    public void SaveRejectsUnsupportedProvider(string provider)
    {
        using var directory = new TestDirectory();
        var store = new SettingsStore(directory.Path);
        var settings = new AppSettings { RuntimeRoot = directory.GetPath("runtime"), Provider = provider };

        Assert.Throws<ArgumentException>(() => store.Save(settings));
    }

    [Theory]
    [InlineData(0)]
    [InlineData(1023)]
    [InlineData(65536)]
    public void SaveRejectsInvalidPorts(int port)
    {
        using var directory = new TestDirectory();
        var store = new SettingsStore(directory.Path);
        var settings = new AppSettings { RuntimeRoot = directory.GetPath("runtime"), WebPort = port };

        Assert.Throws<ArgumentOutOfRangeException>(() => store.Save(settings));
    }

    [Fact]
    public void SaveAllowsEmptyModelDraft()
    {
        using var directory = new TestDirectory();
        var store = new SettingsStore(directory.Path);
        var settings = new AppSettings
        {
            RuntimeRoot = directory.GetPath("runtime"),
            ModelId = string.Empty,
        };

        store.Save(settings);

        Assert.Equal(string.Empty, store.Load().ModelId);
    }

    [Fact]
    public void SaveRejectsRelativeRuntimePath()
    {
        using var directory = new TestDirectory();
        var store = new SettingsStore(directory.Path);
        var settings = new AppSettings { RuntimeRoot = "relative-runtime" };

        Assert.Throws<ArgumentException>(() => store.Save(settings));
    }

    [Fact]
    public void SaveRejectsNegativeNumericValues()
    {
        using var directory = new TestDirectory();
        var store = new SettingsStore(directory.Path);
        var settings = new AppSettings
        {
            RuntimeRoot = directory.GetPath("runtime"),
            DailyBudget = -0.01m,
        };

        Assert.Throws<ArgumentOutOfRangeException>(() => store.Save(settings));
    }
}
