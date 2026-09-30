using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace Vlytics.Settings.Core;

public sealed class RuntimeController
{
    private static readonly HashSet<string> AllowedActions = new(StringComparer.Ordinal)
    {
        "Validate",
        "Apply",
        "Start",
        "Stop",
        "Status",
        "Update",
    };

    private static readonly TimeSpan OperationTimeout = TimeSpan.FromMinutes(10);
    private static readonly HashSet<string> SafeScriptMessages = new(StringComparer.Ordinal)
    {
        "서비스를 중지했습니다. 데이터는 보존됩니다.",
        "운영 설정 파일을 준비했습니다. 시작 전 전체 검증을 수행합니다.",
        "검증을 통과하고 서비스를 시작했습니다.",
        "전체 운영 검증을 통과했습니다. 유료 API 호출은 수행하지 않았습니다.",
        "설정 파일을 읽지 못했거나 다른 작업이 진행 중입니다.",
        "Docker Desktop의 Linux 컨테이너 엔진과 Compose를 확인하세요.",
        "uv와 Python 3.12~3.13 실행 환경을 준비하세요.",
        "설정 파일 생성에 실패했습니다. 이미지 digest, 경로, 예산을 확인하세요.",
        "저장한 설정을 먼저 적용하세요. 저장 이후에는 다시 적용해야 합니다.",
        "Windows 보안 저장소의 데이터베이스 키를 확인하세요.",
        "실제 모델 smoke 검증 후 확인된 모델 버전과 검증 완료 여부를 설정하세요.",
        "확인한 입력·출력 토큰 가격과 KRW/USD 환율을 입력하세요.",
        "선택한 Provider의 API 키를 보안 저장소에 저장하세요.",
        "현재 설정으로 검증한 dry-run 증거 파일을 선택하고 다시 적용하세요.",
        "고정한 이미지 다운로드에 실패했습니다. 이미지 주소와 Docker 접속을 확인하세요.",
        "운영 검증이 실패했습니다. 모델 검증·가격/환율·키·동일 설정의 24시간 내 dry-run·이미지·시각 동기화를 확인하세요.",
        "서비스 시작에 실패했습니다. 컨테이너 상태를 확인하세요. 데이터는 보존됩니다.",
    };
    private static readonly Regex SafeStatusMessage = new(
        "^실행 중인 Vlytics 컨테이너: [0-9]+개$",
        RegexOptions.CultureInvariant | RegexOptions.NonBacktracking);
    private readonly SettingsStore store;
    private readonly SecretVault vault;

    public RuntimeController(SettingsStore store, SecretVault vault)
    {
        this.store = store ?? throw new ArgumentNullException(nameof(store));
        this.vault = vault ?? throw new ArgumentNullException(nameof(vault));
    }

    public async Task<OperationResult> RunAsync(
        string action,
        CancellationToken cancellationToken = default)
    {
        if (!AllowedActions.Contains(action))
        {
            return new OperationResult(false, "The requested operation is not supported.");
        }

        AppSettings settings;
        try
        {
            settings = store.Load();
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or ArgumentException)
        {
            return new OperationResult(false, $"{action} could not load the settings.");
        }

        var runtimeRoot = Path.GetFullPath(settings.RuntimeRoot);
        var scriptPath = Path.GetFullPath(
            Path.Combine(runtimeRoot, "infra", "scripts", "Invoke-DesktopOperation.ps1"));
        if (!File.Exists(scriptPath))
        {
            return new OperationResult(false, $"{action} could not start because the runtime script is missing.");
        }

        var startInfo = CreateStartInfo(action, runtimeRoot, scriptPath, settings.Provider);
        using var process = new Process { StartInfo = startInfo };

        try
        {
            if (!process.Start())
            {
                return new OperationResult(false, $"{action} could not start.");
            }
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or System.ComponentModel.Win32Exception)
        {
            return new OperationResult(false, $"{action} could not start.");
        }

        var standardOutput = process.StandardOutput.ReadToEndAsync(cancellationToken);
        var standardError = process.StandardError.ReadToEndAsync(cancellationToken);
        using var timeout = new CancellationTokenSource(OperationTimeout);
        using var combinedCancellation = CancellationTokenSource.CreateLinkedTokenSource(
            cancellationToken,
            timeout.Token);

        try
        {
            await process.WaitForExitAsync(combinedCancellation.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            KillProcessTree(process);
            await WaitForExitAfterKillAsync(process).ConfigureAwait(false);
            ObserveOutput(standardOutput, standardError);

            return cancellationToken.IsCancellationRequested
                ? new OperationResult(false, $"{action} was canceled.")
                : new OperationResult(false, $"{action} timed out.");
        }

        string output;
        try
        {
            output = await standardOutput.ConfigureAwait(false);
            _ = await standardError.ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            return new OperationResult(false, $"{action} output could not be read.");
        }

        if (!TryParseResult(output, out var scriptResult))
        {
            return new OperationResult(false, $"{action} returned an invalid response.");
        }

        if (!IsSafeScriptMessage(scriptResult.Message!))
        {
            return new OperationResult(false, $"{action} returned an invalid response.");
        }

        if (process.ExitCode != 0 || scriptResult.Success != true)
        {
            return new OperationResult(false, scriptResult.Message!);
        }

        return new OperationResult(true, scriptResult.Message!);
    }

    private ProcessStartInfo CreateStartInfo(string action, string runtimeRoot, string scriptPath, string provider)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            WorkingDirectory = runtimeRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };

        startInfo.ArgumentList.Add("-NoProfile");
        startInfo.ArgumentList.Add("-NonInteractive");
        startInfo.ArgumentList.Add("-File");
        startInfo.ArgumentList.Add(scriptPath);
        startInfo.ArgumentList.Add("-Action");
        startInfo.ArgumentList.Add(action);
        startInfo.ArgumentList.Add("-SettingsFile");
        startInfo.ArgumentList.Add(store.SettingsPath);
        startInfo.ArgumentList.Add("-DataDirectory");
        startInfo.ArgumentList.Add(store.DataDirectory);

        foreach (var name in SecretVault.AllowedNames)
        {
            startInfo.Environment.Remove(name);
        }
        foreach (var name in new[] { "MIGRATOR_DATABASE_URL", "COLLECTOR_DATABASE_URL",
            "ENGINE_DATABASE_URL", "MARKET_INGEST_DATABASE_URL", "READ_API_DATABASE_URL",
            "VLYTICS_DATABASE_URL", "VLYTICS_BACKUP_CREDENTIAL", "VLYTICS_ALERT_DESTINATION" })
        {
            startInfo.Environment.Remove(name);
        }

        if (action is "Validate" or "Start" or "Update")
        {
            var providerKey = $"VLYTICS_{provider.ToUpperInvariant()}_API_KEY";
            foreach (var name in SecretVault.AllowedNames)
            {
                if (name.EndsWith("_API_KEY", StringComparison.Ordinal) && name != providerKey)
                {
                    continue;
                }
                var value = vault.Read(name);
                if (value is not null) { startInfo.Environment[name] = value; }
            }
        }

        return startInfo;
    }

    private static bool TryParseResult(string output, out ScriptResult result)
    {
        result = new ScriptResult();
        var lastLine = output
            .Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .LastOrDefault();
        if (lastLine is null || lastLine.Length > 2048)
        {
            return false;
        }

        try
        {
            var parsed = JsonSerializer.Deserialize<ScriptResult>(lastLine);
            if (parsed?.Success is null || parsed.Message is null || parsed.Message.Length > 512)
            {
                return false;
            }

            result = parsed;
            return true;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    private static bool IsSafeScriptMessage(string message)
    {
        return SafeScriptMessages.Contains(message) || SafeStatusMessage.IsMatch(message);
    }

    private static void KillProcessTree(Process process)
    {
        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch (Exception exception) when (exception is InvalidOperationException or System.ComponentModel.Win32Exception)
        {
        }
    }

    private static async Task WaitForExitAfterKillAsync(Process process)
    {
        try
        {
            await process.WaitForExitAsync().ConfigureAwait(false);
        }
        catch (InvalidOperationException)
        {
        }
    }

    private static void ObserveOutput(Task<string> standardOutput, Task<string> standardError)
    {
        _ = standardOutput.ContinueWith(
            task => _ = task.Exception,
            CancellationToken.None,
            TaskContinuationOptions.OnlyOnFaulted | TaskContinuationOptions.ExecuteSynchronously,
            TaskScheduler.Default);
        _ = standardError.ContinueWith(
            task => _ = task.Exception,
            CancellationToken.None,
            TaskContinuationOptions.OnlyOnFaulted | TaskContinuationOptions.ExecuteSynchronously,
            TaskScheduler.Default);
    }

    private sealed class ScriptResult
    {
        [JsonPropertyName("success")]
        public bool? Success { get; init; }

        [JsonPropertyName("message")]
        public string? Message { get; init; }
    }
}

public sealed record OperationResult(bool Success, string Message);
