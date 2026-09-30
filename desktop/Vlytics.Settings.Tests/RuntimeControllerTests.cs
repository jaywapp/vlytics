using System.Text;
using Vlytics.Settings.Core;

namespace Vlytics.Settings.Tests;

public sealed class RuntimeControllerTests
{
    [Fact]
    public async Task RunAsyncPassesSecretsOnlyThroughEnvironmentAndSanitizesScriptMessage()
    {
        using var directory = new TestDirectory();
        var runtimeRoot = directory.GetPath("runtime");
        var scriptDirectory = System.IO.Path.Combine(runtimeRoot, "infra", "scripts");
        Directory.CreateDirectory(scriptDirectory);
        var scriptPath = System.IO.Path.Combine(scriptDirectory, "Invoke-DesktopOperation.ps1");
        const string secret = "api-test-secret-fc3fc4b0";
        File.WriteAllText(
            scriptPath,
            """
            param(
                [string]$Action,
                [string]$SettingsFile,
                [string]$DataDirectory
            )
            [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
            $secretInArguments = $MyInvocation.Line.Contains($env:VLYTICS_OPENAI_API_KEY)
            $valid = $Action -eq 'Validate' -and
                (Test-Path -LiteralPath $SettingsFile) -and
                $DataDirectory -and
                $env:VLYTICS_OPENAI_API_KEY -and
                -not $secretInArguments
            Write-Output 'diagnostic output that must not be returned'
            [ordered]@{ success = [bool]$valid; message = '실행 중인 Vlytics 컨테이너: 2개' } |
                ConvertTo-Json -Compress
            """,
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: true));

        var store = new SettingsStore(directory.GetPath("data"));
        store.Save(new AppSettings { RuntimeRoot = runtimeRoot });
        var vault = new SecretVault(store.DataDirectory);
        vault.Save("VLYTICS_OPENAI_API_KEY", secret);
        var controller = new RuntimeController(store, vault);

        var result = await controller.RunAsync("Validate");

        Assert.True(result.Success);
        Assert.Equal("실행 중인 Vlytics 컨테이너: 2개", result.Message);
        Assert.DoesNotContain(secret, result.Message, StringComparison.Ordinal);
        Assert.DoesNotContain("diagnostic", result.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task RunAsyncRejectsArbitraryJsonMessageWithoutReturningSecret()
    {
        using var directory = new TestDirectory();
        var runtimeRoot = directory.GetPath("runtime");
        var scriptDirectory = System.IO.Path.Combine(runtimeRoot, "infra", "scripts");
        Directory.CreateDirectory(scriptDirectory);
        const string secret = "api-test-secret-a722aca3";
        File.WriteAllText(
            System.IO.Path.Combine(scriptDirectory, "Invoke-DesktopOperation.ps1"),
            "[ordered]@{ success = $true; message = $env:VLYTICS_OPENAI_API_KEY } | ConvertTo-Json -Compress");
        var store = new SettingsStore(directory.GetPath("data"));
        store.Save(new AppSettings { RuntimeRoot = runtimeRoot });
        var vault = new SecretVault(store.DataDirectory);
        vault.Save("VLYTICS_OPENAI_API_KEY", secret);
        var controller = new RuntimeController(store, vault);

        var result = await controller.RunAsync("Validate");

        Assert.False(result.Success);
        Assert.Equal("Validate returned an invalid response.", result.Message);
        Assert.DoesNotContain(secret, result.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task RunAsyncRejectsUnknownActionBeforeStartingProcess()
    {
        using var directory = new TestDirectory();
        var store = new SettingsStore(directory.GetPath("data"));
        store.Save(new AppSettings { RuntimeRoot = directory.GetPath("missing-runtime") });
        var controller = new RuntimeController(store, new SecretVault(store.DataDirectory));

        var result = await controller.RunAsync("DeleteEverything");

        Assert.False(result.Success);
        Assert.Equal("The requested operation is not supported.", result.Message);
    }

    [Fact]
    public async Task RunAsyncRejectsResponseWhenLastLineIsNotGuardedJson()
    {
        using var directory = new TestDirectory();
        var runtimeRoot = directory.GetPath("runtime");
        var scriptDirectory = System.IO.Path.Combine(runtimeRoot, "infra", "scripts");
        Directory.CreateDirectory(scriptDirectory);
        File.WriteAllText(
            System.IO.Path.Combine(scriptDirectory, "Invoke-DesktopOperation.ps1"),
            "Write-Output '{\"success\":true,\"message\":\"ok\"}'\nWrite-Output 'unsafe trailing text'");
        var store = new SettingsStore(directory.GetPath("data"));
        store.Save(new AppSettings { RuntimeRoot = runtimeRoot });
        var controller = new RuntimeController(store, new SecretVault(store.DataDirectory));

        var result = await controller.RunAsync("Validate");

        Assert.False(result.Success);
        Assert.Equal("Validate returned an invalid response.", result.Message);
    }

    [Theory]
    [InlineData("Status")]
    [InlineData("Stop")]
    [InlineData("Apply")]
    public async Task NonSecretOperationsWorkWithCorruptedVault(string action)
    {
        using var directory = new TestDirectory();
        var runtimeRoot = directory.GetPath("runtime");
        var scriptDirectory = System.IO.Path.Combine(runtimeRoot, "infra", "scripts");
        Directory.CreateDirectory(scriptDirectory);
        File.WriteAllText(
            System.IO.Path.Combine(scriptDirectory, "Invoke-DesktopOperation.ps1"),
            """
            [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
            $valid = -not $env:VLYTICS_OPENAI_API_KEY
            @{ success = [bool]$valid; message = '실행 중인 Vlytics 컨테이너: 0개' } | ConvertTo-Json -Compress
            """, new UTF8Encoding(true));
        var store = new SettingsStore(directory.GetPath("data"));
        store.Save(new AppSettings { RuntimeRoot = runtimeRoot });
        var vault = new SecretVault(store.DataDirectory);
        vault.Save("VLYTICS_OPENAI_API_KEY", "synthetic-unused-secret");
        var ciphertext = Directory.GetFiles(store.DataDirectory, "*.bin", SearchOption.AllDirectories).Single();
        File.WriteAllBytes(ciphertext, [0, 1, 2]);
        var result = await new RuntimeController(store, vault).RunAsync(action);
        Assert.True(result.Success);
    }
}
