using System.Diagnostics;
using System.IO;
using System.Windows;
using Vlytics.Settings.Core;

namespace Vlytics.Settings.Desktop;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        if (e.Args.Any(argument => string.Equals(argument, "--self-check", StringComparison.OrdinalIgnoreCase)))
        {
            RunSelfCheck(
                TryResolveOption(e.Args, "--data-directory"),
                TryResolveOption(e.Args, "--snapshot"));
            return;
        }

        try
        {
            var dataDirectory = TryResolveOption(e.Args, "--data-directory") ?? Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Vlytics");
            var store = new SettingsStore(dataDirectory);
            var vault = new SecretVault(dataDirectory);
            vault.InitializeDefaults();
            var runtime = new RuntimeController(store, vault);
            var window = new MainWindow(store, vault, runtime);
            MainWindow = window;
            window.Show();
        }
        catch
        {
            MessageBox.Show(
                "Vlytics 설정 앱을 초기화하지 못했습니다. 로컬 데이터 폴더 권한을 확인한 뒤 다시 실행하세요.",
                "Vlytics 설정",
                MessageBoxButton.OK,
                MessageBoxImage.Error);
            Shutdown(1);
        }
    }

    private static string? TryResolveOption(IReadOnlyList<string> arguments, string option)
    {
        for (var index = 0; index < arguments.Count; index++)
        {
            if (!string.Equals(arguments[index], option, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            if (index + 1 >= arguments.Count || string.IsNullOrWhiteSpace(arguments[index + 1]))
            {
                throw new ArgumentException($"The {option} option requires a path.");
            }

            var value = arguments[index + 1];
            if (!Path.IsPathFullyQualified(value))
            {
                throw new ArgumentException($"The {option} path must be absolute.");
            }

            return Path.GetFullPath(value);
        }

        return null;
    }

    private void RunSelfCheck(string? dataDirectoryOverride, string? snapshotPath)
    {
        var selfCheckDirectory = dataDirectoryOverride ?? Path.Combine(
            Path.GetTempPath(), $"vlytics-settings-self-check-{Guid.NewGuid():N}");
        var ownsSelfCheckDirectory = dataDirectoryOverride is null;
        var exitCode = 1;

        try
        {
            var store = new SettingsStore(selfCheckDirectory);
            var vault = new SecretVault(selfCheckDirectory);
            var runtime = new RuntimeController(store, vault);
            var window = new MainWindow(store, vault, runtime);
            exitCode = window.RunSelfCheck(snapshotPath) ? 0 : 1;
            window.Close();
            Trace.WriteLine(exitCode == 0 ? "VLYTICS_SETTINGS_SELF_CHECK_OK" : "VLYTICS_SETTINGS_SELF_CHECK_FAILED");
        }
        catch
        {
            Trace.WriteLine("VLYTICS_SETTINGS_SELF_CHECK_FAILED");
        }
        finally
        {
            try
            {
                if (ownsSelfCheckDirectory && Directory.Exists(selfCheckDirectory))
                {
                    Directory.Delete(selfCheckDirectory, true);
                }
            }
            catch
            {
                // A self-check cleanup failure must not expose local path details.
            }
        }

        Shutdown(exitCode);
    }
}
