using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Microsoft.Win32;
using Vlytics.Settings.Core;

namespace Vlytics.Settings.Desktop;

public partial class MainWindow : Window
{
    private const string ValidateAction = "Validate";
    private const string ApplyAction = "Apply";
    private const string StartAction = "Start";
    private const string StopAction = "Stop";
    private const string StatusAction = "Status";
    private const string UpdateAction = "Update";
    private const string OpenAiKey = "VLYTICS_OPENAI_API_KEY";
    private const string AnthropicKey = "VLYTICS_ANTHROPIC_API_KEY";
    private const string GoogleKey = "VLYTICS_GOOGLE_API_KEY";
    private const string OperatorSecret = "VLYTICS_OPERATOR_AUTH_SECRET";
    private const string ReadonlySecret = "VLYTICS_READONLY_AUTH_SECRET";
    private const string MigratorDatabasePassword = "MIGRATOR_DATABASE_PASSWORD";
    private const string CollectorDatabasePassword = "COLLECTOR_DATABASE_PASSWORD";
    private const string EngineDatabasePassword = "ENGINE_DATABASE_PASSWORD";
    private const string MarketIngestDatabasePassword = "MARKET_INGEST_DATABASE_PASSWORD";
    private const string ReadApiDatabasePassword = "READ_API_DATABASE_PASSWORD";

    private static readonly string[] ImageKeys =
    [
        "VLYTICS_BACKEND_IMAGE",
        "VLYTICS_FRONTEND_IMAGE",
        "VLYTICS_NODE_BUILD_IMAGE",
        "VLYTICS_NGINX_RUNTIME_IMAGE",
        "VLYTICS_POSTGRES_IMAGE",
        "VLYTICS_PYTHON_BUILD_IMAGE",
        "VLYTICS_UV_BUILD_IMAGE"
    ];

    private readonly SettingsStore _settingsStore;
    private readonly SecretVault _secretVault;
    private readonly RuntimeController _runtimeController;
    private AppSettings _savedSettings = new();
    private CancellationTokenSource? _operationCancellation;
    private string _lastSavedFingerprint = string.Empty;
    private bool _loading = true;
    private bool _busy;
    private bool _providerStatusSuppressed;
    private bool _closeAfterOperation;
    private bool _allowClose;
    private bool _settingsPersisted;

    public MainWindow(
        SettingsStore settingsStore,
        SecretVault secretVault,
        RuntimeController runtimeController)
    {
        _settingsStore = settingsStore;
        _secretVault = secretVault;
        _runtimeController = runtimeController;

        InitializeComponent();
        RegisterDirtyTracking();
        LoadSettings();
    }

    public bool RunSelfCheck(string? snapshotPath = null)
    {
        for (var index = 0; index < MainTabs.Items.Count; index++)
        {
            MainTabs.SelectedIndex = index;
            Measure(new Size(1180, 820));
            Arrange(new Rect(0, 0, 1180, 820));
            UpdateLayout();
        }

        MainTabs.SelectedIndex = 0;
        Measure(new Size(1180, 820));
        Arrange(new Rect(0, 0, 1180, 820));
        UpdateLayout();

        if (!string.IsNullOrWhiteSpace(snapshotPath))
        {
            SaveSnapshot(snapshotPath);
        }

        var initialSettings = _settingsStore.Load();
        SaveButton_Click(this, new RoutedEventArgs());
        var persistedSettings = _settingsStore.Load();
        var saveRoundTripPassed = persistedSettings.Provider == initialSettings.Provider
            && persistedSettings.DailyBudget == initialSettings.DailyBudget
            && ProviderKeyBox.SecurePassword.Length == 0
            && OperatorSecretBox.SecurePassword.Length == 0
            && ReadonlySecretBox.SecurePassword.Length == 0;

        ModelVerifiedBox.IsChecked = true;
        PinnedModelVersionBox.Text = "self-check-version";
        InputPriceBox.Text = "1";
        OutputPriceBox.Text = "2";
        ProviderBox.SelectedIndex = 1;
        var providerResetPassed = ModelVerifiedBox.IsChecked == false
            && string.IsNullOrEmpty(PinnedModelVersionBox.Text)
            && string.IsNullOrEmpty(InputPriceBox.Text)
            && string.IsNullOrEmpty(OutputPriceBox.Text)
            && string.IsNullOrEmpty(ModelIdBox.Text);

        PopulateForm(initialSettings);
        _savedSettings = initialSettings;
        _lastSavedFingerprint = BuildFingerprint();

        return MainTabs.Items.Count == 4
            && ProviderBox.Items.Count == 3
            && WebPortBox.Text.Length > 0
            && ImageKeys.All(key => _savedSettings.Images.ContainsKey(key))
            && new[] { ValidateAction, ApplyAction, StartAction, StopAction, StatusAction, UpdateAction }
                .SequenceEqual(["Validate", "Apply", "Start", "Stop", "Status", "Update"])
            && saveRoundTripPassed
            && providerResetPassed
            && Application.Current.TryFindResource("PrimaryButtonStyle") is not null
            && Application.Current.TryFindResource("CardStyle") is not null;
    }

    private bool HasUnsavedChanges => !_loading && (!_settingsPersisted || !string.Equals(
        BuildFingerprint(), _lastSavedFingerprint, StringComparison.Ordinal));

    private void RegisterDirtyTracking()
    {
        AddHandler(TextBox.TextChangedEvent, new TextChangedEventHandler(InputChanged));
        AddHandler(CheckBox.CheckedEvent, new RoutedEventHandler(InputChanged));
        AddHandler(CheckBox.UncheckedEvent, new RoutedEventHandler(InputChanged));
    }

    private void LoadSettings()
    {
        try
        {
            _savedSettings = _settingsStore.Load();
            _settingsPersisted = File.Exists(_settingsStore.SettingsPath);
            PopulateForm(_savedSettings);
            _lastSavedFingerprint = BuildFingerprint();
            UpdateSecretStatuses();
            SetSaveState(
                _settingsPersisted ? "저장된 설정을 불러왔습니다." : "기본값을 불러왔습니다. 운영 전에 설정을 저장하세요.",
                !_settingsPersisted);
        }
        catch
        {
            _savedSettings = new AppSettings();
            _settingsPersisted = false;
            PopulateForm(_savedSettings);
            _lastSavedFingerprint = BuildFingerprint();
            SetSaveState("설정을 불러오지 못했습니다. 입력 값을 확인한 뒤 다시 저장하세요.", true);
        }
        finally
        {
            _loading = false;
            RefreshButtonState();
        }
    }

    private void PopulateForm(AppSettings settings)
    {
        _loading = true;
        ProviderBox.SelectedIndex = settings.Provider switch
        {
            "anthropic" => 1,
            "google" => 2,
            _ => 0
        };
        ModelIdBox.Text = settings.ModelId;
        PinnedModelVersionBox.Text = settings.PinnedModelVersion;
        ModelVerifiedBox.IsChecked = settings.ModelVerified;
        PricingRateBox.Text = FormatDecimal(settings.PricingToBudgetRate);
        InputPriceBox.Text = FormatDecimal(settings.InputPrice);
        OutputPriceBox.Text = FormatDecimal(settings.OutputPrice);
        DailyBudgetBox.Text = FormatDecimal(settings.DailyBudget);
        MonthlyBudgetBox.Text = FormatDecimal(settings.MonthlyBudget);
        MaxCallsBox.Text = settings.MaxCallsPerMatch.ToString(CultureInfo.CurrentCulture);
        DailyCallsBox.Text = settings.DailyCallLimit.ToString(CultureInfo.CurrentCulture);
        MonthlyCallsBox.Text = settings.MonthlyCallLimit.ToString(CultureInfo.CurrentCulture);
        MaxInputTokensBox.Text = settings.MaxInputTokens.ToString(CultureInfo.CurrentCulture);
        MaxOutputTokensBox.Text = settings.MaxOutputTokens.ToString(CultureInfo.CurrentCulture);
        WebPortBox.Text = settings.WebPort.ToString(CultureInfo.CurrentCulture);
        RuntimeRootBox.Text = settings.RuntimeRoot;
        EvidencePathBox.Text = settings.DryRunEvidencePath;
        SshHostBox.Text = settings.SshHost;
        SshUserBox.Text = settings.SshUser;
        SshPortBox.Text = settings.SshPort.ToString(CultureInfo.CurrentCulture);

        BackendImageBox.Text = ReadImage(settings, ImageKeys[0]);
        FrontendImageBox.Text = ReadImage(settings, ImageKeys[1]);
        NodeImageBox.Text = ReadImage(settings, ImageKeys[2]);
        NginxImageBox.Text = ReadImage(settings, ImageKeys[3]);
        PostgresImageBox.Text = ReadImage(settings, ImageKeys[4]);
        PythonImageBox.Text = ReadImage(settings, ImageKeys[5]);
        UvImageBox.Text = ReadImage(settings, ImageKeys[6]);
        _loading = false;
    }

    private static string ReadImage(AppSettings settings, string key) =>
        settings.Images.TryGetValue(key, out var value) ? value : string.Empty;

    private static string FormatDecimal(decimal value) => value.ToString("0.################", CultureInfo.CurrentCulture);

    private void InputChanged(object sender, RoutedEventArgs e)
    {
        if (_loading)
        {
            return;
        }

        SetSaveState(HasUnsavedChanges
            ? "저장하지 않은 변경 사항이 있습니다. 시작·업데이트 전에 저장하고 적용하세요."
            : "모든 변경 사항이 저장되었습니다.", HasUnsavedChanges);
        RefreshButtonState();
    }

    private void SecretBox_PasswordChanged(object sender, RoutedEventArgs e) => InputChanged(sender, e);

    private void ProviderBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_loading || ProviderBox.SelectedItem is null)
        {
            return;
        }

        _loading = true;
        ModelIdBox.Clear();
        PinnedModelVersionBox.Clear();
        ModelVerifiedBox.IsChecked = false;
        InputPriceBox.Clear();
        OutputPriceBox.Clear();
        ProviderKeyBox.Clear();
        ProviderKeyStatusText.Text = "공급자가 변경되었습니다. 저장 후 해당 키 상태를 다시 확인합니다.";
        _providerStatusSuppressed = true;
        _loading = false;
        InputChanged(sender, e);
    }

    private string SelectedProvider => ProviderBox.SelectedIndex switch
    {
        1 => "anthropic",
        2 => "google",
        _ => "openai"
    };

    private string SelectedProviderLabel => ProviderBox.SelectedIndex switch
    {
        1 => "Anthropic",
        2 => "Google Gemini",
        _ => "OpenAI"
    };

    private string SelectedProviderSecretName => SelectedProvider switch
    {
        "anthropic" => AnthropicKey,
        "google" => GoogleKey,
        _ => OpenAiKey
    };

    private string BuildFingerprint()
    {
        var values = new[]
        {
            SelectedProvider, ModelIdBox.Text, PinnedModelVersionBox.Text,
            ModelVerifiedBox.IsChecked == true ? "1" : "0", PricingRateBox.Text,
            InputPriceBox.Text, OutputPriceBox.Text, DailyBudgetBox.Text, MonthlyBudgetBox.Text,
            MaxCallsBox.Text, DailyCallsBox.Text, MonthlyCallsBox.Text, MaxInputTokensBox.Text,
            MaxOutputTokensBox.Text, WebPortBox.Text, RuntimeRootBox.Text, EvidencePathBox.Text,
            SshHostBox.Text, SshUserBox.Text, SshPortBox.Text, BackendImageBox.Text,
            FrontendImageBox.Text, NodeImageBox.Text, NginxImageBox.Text, PostgresImageBox.Text,
            PythonImageBox.Text, UvImageBox.Text,
            ProviderKeyBox.SecurePassword.Length > 0 ? "provider-secret-pending" : string.Empty,
            OperatorSecretBox.SecurePassword.Length > 0 ? "operator-secret-pending" : string.Empty,
            ReadonlySecretBox.SecurePassword.Length > 0 ? "readonly-secret-pending" : string.Empty,
            MigratorPasswordBox.SecurePassword.Length > 0 ? "migrator-secret-pending" : string.Empty,
            CollectorPasswordBox.SecurePassword.Length > 0 ? "collector-secret-pending" : string.Empty,
            EnginePasswordBox.SecurePassword.Length > 0 ? "engine-secret-pending" : string.Empty,
            MarketIngestPasswordBox.SecurePassword.Length > 0 ? "market-secret-pending" : string.Empty,
            ReadApiPasswordBox.SecurePassword.Length > 0 ? "read-api-secret-pending" : string.Empty
        };
        return string.Join('\u001f', values);
    }

    private bool TryBuildSettings(out AppSettings settings, out string validationMessage)
    {
        var errors = new List<string>();

        var pricingRate = ReadDecimal(PricingRateBox, "USD→KRW 환산율", errors);
        var inputPrice = ReadDecimal(InputPriceBox, "입력 가격", errors);
        var outputPrice = ReadDecimal(OutputPriceBox, "출력 가격", errors);
        var dailyBudget = ReadDecimal(DailyBudgetBox, "일일 예산", errors);
        var monthlyBudget = ReadDecimal(MonthlyBudgetBox, "월간 예산", errors);
        var maxCalls = ReadInt(MaxCallsBox, "경기당 최대 호출", errors);
        var dailyCalls = ReadInt(DailyCallsBox, "일일 호출 한도", errors);
        var monthlyCalls = ReadInt(MonthlyCallsBox, "월간 호출 한도", errors);
        var maxInput = ReadInt(MaxInputTokensBox, "최대 입력 토큰", errors);
        var maxOutput = ReadInt(MaxOutputTokensBox, "최대 출력 토큰", errors);
        var webPort = ReadInt(WebPortBox, "웹 포트", errors);
        var sshPort = ReadInt(SshPortBox, "SSH 포트", errors);

        if (string.IsNullOrWhiteSpace(ModelIdBox.Text))
        {
            errors.Add("모델 ID를 입력하세요.");
        }

        if (ModelVerifiedBox.IsChecked == true
            && (string.IsNullOrWhiteSpace(PinnedModelVersionBox.Text)
                || string.IsNullOrWhiteSpace(EvidencePathBox.Text)))
        {
            errors.Add("모델 검증 완료에는 고정 버전과 근거 파일이 필요합니다.");
        }

        if (webPort is < 1024 or > 65535)
        {
            errors.Add("웹 포트는 1024~65535 범위여야 합니다.");
        }

        if (sshPort is < 1 or > 65535)
        {
            errors.Add("SSH 포트는 1~65535 범위여야 합니다.");
        }

        if (new[] { pricingRate, inputPrice, outputPrice, dailyBudget, monthlyBudget }.Any(value => value < 0))
        {
            errors.Add("가격, 환산율, 예산은 음수일 수 없습니다.");
        }

        if (new[] { maxCalls, dailyCalls, monthlyCalls, maxInput, maxOutput }.Any(value => value < 1))
        {
            errors.Add("호출 및 토큰 한도는 1 이상이어야 합니다.");
        }

        settings = new AppSettings
        {
            RuntimeRoot = RuntimeRootBox.Text.Trim(),
            Provider = SelectedProvider,
            ModelId = ModelIdBox.Text.Trim(),
            PinnedModelVersion = PinnedModelVersionBox.Text.Trim(),
            ModelVerified = ModelVerifiedBox.IsChecked == true,
            PricingToBudgetRate = pricingRate,
            InputPrice = inputPrice,
            OutputPrice = outputPrice,
            DailyBudget = dailyBudget,
            MonthlyBudget = monthlyBudget,
            MaxCallsPerMatch = maxCalls,
            DailyCallLimit = dailyCalls,
            MonthlyCallLimit = monthlyCalls,
            MaxInputTokens = maxInput,
            MaxOutputTokens = maxOutput,
            WebPort = webPort,
            DryRunEvidencePath = EvidencePathBox.Text.Trim(),
            SshHost = SshHostBox.Text.Trim(),
            SshUser = SshUserBox.Text.Trim(),
            SshPort = sshPort,
            Images = new Dictionary<string, string>(StringComparer.Ordinal)
            {
                [ImageKeys[0]] = BackendImageBox.Text.Trim(),
                [ImageKeys[1]] = FrontendImageBox.Text.Trim(),
                [ImageKeys[2]] = NodeImageBox.Text.Trim(),
                [ImageKeys[3]] = NginxImageBox.Text.Trim(),
                [ImageKeys[4]] = PostgresImageBox.Text.Trim(),
                [ImageKeys[5]] = PythonImageBox.Text.Trim(),
                [ImageKeys[6]] = UvImageBox.Text.Trim()
            }
        };

        validationMessage = string.Join(Environment.NewLine, errors.Distinct());
        return errors.Count == 0;
    }

    private static decimal ReadDecimal(TextBox box, string label, ICollection<string> errors)
    {
        if (decimal.TryParse(box.Text, NumberStyles.Number, CultureInfo.CurrentCulture, out var value)
            || decimal.TryParse(box.Text, NumberStyles.Number, CultureInfo.InvariantCulture, out value))
        {
            return value;
        }

        errors.Add($"{label}에 올바른 숫자를 입력하세요.");
        return 0;
    }

    private static int ReadInt(TextBox box, string label, ICollection<string> errors)
    {
        if (int.TryParse(box.Text, NumberStyles.Integer, CultureInfo.CurrentCulture, out var value))
        {
            return value;
        }

        errors.Add($"{label}에 올바른 정수를 입력하세요.");
        return 0;
    }

    private void SaveButton_Click(object sender, RoutedEventArgs e)
    {
        if (!TryBuildSettings(out var settings, out var validationMessage))
        {
            SetSaveState(validationMessage, true);
            MainTabs.SelectedIndex = validationMessage.Contains("모델", StringComparison.Ordinal) ? 1 : 0;
            return;
        }

        try
        {
            _settingsStore.Save(settings);
            SaveSecretIfEntered(SelectedProviderSecretName, ProviderKeyBox);
            SaveSecretIfEntered(OperatorSecret, OperatorSecretBox);
            SaveSecretIfEntered(ReadonlySecret, ReadonlySecretBox);
            SaveSecretIfEntered(MigratorDatabasePassword, MigratorPasswordBox);
            SaveSecretIfEntered(CollectorDatabasePassword, CollectorPasswordBox);
            SaveSecretIfEntered(EngineDatabasePassword, EnginePasswordBox);
            SaveSecretIfEntered(MarketIngestDatabasePassword, MarketIngestPasswordBox);
            SaveSecretIfEntered(ReadApiDatabasePassword, ReadApiPasswordBox);

            _savedSettings = settings;
            _settingsPersisted = true;
            ProviderKeyBox.Clear();
            OperatorSecretBox.Clear();
            ReadonlySecretBox.Clear();
            MigratorPasswordBox.Clear();
            CollectorPasswordBox.Clear();
            EnginePasswordBox.Clear();
            MarketIngestPasswordBox.Clear();
            ReadApiPasswordBox.Clear();
            _providerStatusSuppressed = false;
            _lastSavedFingerprint = BuildFingerprint();
            UpdateSecretStatuses();
            SetSaveState("설정과 입력한 새 비밀번호를 안전하게 저장했습니다. 아직 런타임에는 적용되지 않았습니다.", false);
            ApplyStateText.Text = "저장 완료 · 설정 검증과 적용이 필요합니다. 실행 중인 서비스는 아직 기존 설정을 사용합니다.";
            AppendOperationLog("저장", "설정을 저장했습니다. 검증과 적용은 별도로 실행하세요.");
        }
        catch
        {
            SetSaveState("설정을 저장하지 못했습니다. 로컬 데이터 폴더 권한을 확인하세요.", true);
        }

        RefreshButtonState();
    }

    private void SaveSecretIfEntered(string name, PasswordBox box)
    {
        if (box.SecurePassword.Length > 0)
        {
            _secretVault.Save(name, box.Password);
        }
    }

    private void UpdateSecretStatuses()
    {
        if (!_providerStatusSuppressed)
        {
            ProviderKeyStatusText.Text = _secretVault.Contains(SelectedProviderSecretName)
                ? $"{SelectedProviderLabel} 키가 Windows 보호 저장소에 저장되어 있습니다."
                : $"{SelectedProviderLabel} 키가 아직 저장되지 않았습니다.";
        }

        OperatorSecretStatusText.Text = _secretVault.Contains(OperatorSecret)
            ? "운영자 비밀번호가 Windows 보호 저장소에 저장되어 있습니다."
            : "운영자 비밀번호가 아직 저장되지 않았습니다.";
        ReadonlySecretStatusText.Text = _secretVault.Contains(ReadonlySecret)
            ? "읽기 전용 비밀번호가 Windows 보호 저장소에 저장되어 있습니다."
            : "읽기 전용 비밀번호는 저장되지 않았습니다.";
    }

    private void DeleteProviderKeyButton_Click(object sender, RoutedEventArgs e)
    {
        var answer = MessageBox.Show(
            $"저장된 {SelectedProviderLabel} API 키를 삭제할까요? 이 작업은 되돌릴 수 없습니다.",
            "공급자 키 삭제",
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning,
            MessageBoxResult.No);
        if (answer != MessageBoxResult.Yes)
        {
            return;
        }

        try
        {
            _secretVault.Delete(SelectedProviderSecretName);
            ProviderKeyBox.Clear();
            _providerStatusSuppressed = false;
            UpdateSecretStatuses();
            AppendOperationLog("키 삭제", $"{SelectedProviderLabel} API 키를 삭제했습니다.");
        }
        catch
        {
            SetSaveState("저장된 API 키를 삭제하지 못했습니다.", true);
        }
    }

    private async void ValidateButton_Click(object sender, RoutedEventArgs e) => await RunOperationAsync(ValidateAction, "설정 검증");
    private async void ApplyButton_Click(object sender, RoutedEventArgs e) => await RunOperationAsync(ApplyAction, "설정 적용");
    private async void StartButton_Click(object sender, RoutedEventArgs e) => await RunOperationAsync(StartAction, "서비스 시작");
    private async void StopButton_Click(object sender, RoutedEventArgs e) => await RunOperationAsync(StopAction, "서비스 중지", false);
    private async void StatusButton_Click(object sender, RoutedEventArgs e) => await RunOperationAsync(StatusAction, "상태 조회", false);
    private async void UpdateButton_Click(object sender, RoutedEventArgs e) => await RunOperationAsync(UpdateAction, "이미지 업데이트");

    private async Task RunOperationAsync(string action, string label, bool requiresSavedSettings = true)
    {
        if (_busy)
        {
            return;
        }

        if (requiresSavedSettings && HasUnsavedChanges)
        {
            SetSaveState("먼저 변경 사항을 저장한 뒤 다시 실행하세요.", true);
            return;
        }

        _operationCancellation = new CancellationTokenSource();
        SetBusy(true, $"{label} 작업 중…");
        AppendOperationLog(label, "작업을 시작했습니다.");

        try
        {
            var result = await _runtimeController.RunAsync(action, _operationCancellation.Token);
            var message = string.IsNullOrWhiteSpace(result.Message)
                ? (result.Success ? "작업을 완료했습니다." : "작업을 완료하지 못했습니다.")
                : LocalizeOperationMessage(result.Message);
            RuntimeStateText.Text = message;
            AppendOperationLog(label, message);

            if (result.Success && action == ApplyAction)
            {
                ApplyStateText.Text = "저장된 설정을 런타임에 반영했습니다. 이미 실행 중인 서비스는 재시작 전까지 이전 설정을 사용할 수 있습니다.";
            }
            else if (result.Success && action == StartAction)
            {
                ApplyStateText.Text = "서비스를 저장·적용된 설정으로 시작했습니다.";
            }
        }
        catch (OperationCanceledException)
        {
            RuntimeStateText.Text = "작업을 취소했습니다.";
            AppendOperationLog(label, "사용자가 작업을 취소했습니다.");
        }
        catch
        {
            RuntimeStateText.Text = "작업 중 오류가 발생했습니다. 설정과 Docker 상태를 확인하세요.";
            AppendOperationLog(label, "예기치 않은 오류로 작업을 완료하지 못했습니다.");
        }
        finally
        {
            _operationCancellation.Dispose();
            _operationCancellation = null;
            SetBusy(false, HasUnsavedChanges ? "저장하지 않은 변경 사항이 있습니다." : "작업이 끝났습니다.");
            if (_closeAfterOperation)
            {
                _ = Dispatcher.BeginInvoke(() =>
                {
                    _allowClose = true;
                    Close();
                });
            }
        }
    }

    private void CancelButton_Click(object sender, RoutedEventArgs e) => _operationCancellation?.Cancel();

    private void SetBusy(bool busy, string status)
    {
        _busy = busy;
        BusyProgress.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        SetSaveState(status, HasUnsavedChanges);
        RefreshButtonState();
    }

    private void RefreshButtonState()
    {
        var clean = !HasUnsavedChanges;
        SaveButton.IsEnabled = !_busy;
        ValidateButton.IsEnabled = !_busy && clean;
        ApplyButton.IsEnabled = !_busy && clean;
        StartButton.IsEnabled = !_busy && clean;
        UpdateButton.IsEnabled = !_busy && clean;
        StopButton.IsEnabled = !_busy;
        StatusButton.IsEnabled = !_busy;
        OpenBrowserButton.IsEnabled = !_busy;
        DeleteProviderKeyButton.IsEnabled = !_busy;
        CancelButton.IsEnabled = _busy;
    }

    private void SetSaveState(string message, bool attention)
    {
        SaveStateText.Text = message;
        SaveStateText.Foreground = attention
            ? new System.Windows.Media.SolidColorBrush(System.Windows.Media.Color.FromRgb(180, 83, 9))
            : (System.Windows.Media.Brush)FindResource("MutedBrush");
    }

    private void AppendOperationLog(string label, string message)
    {
        var builder = new StringBuilder(OperationLogBox.Text);
        if (builder.Length > 0)
        {
            builder.AppendLine();
        }

        builder.Append(CultureInfo.CurrentCulture.DateTimeFormat.ShortTimePattern.Contains('t')
            ? DateTime.Now.ToString("g", CultureInfo.CurrentCulture)
            : DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.CurrentCulture));
        builder.Append("  ·  ");
        builder.Append(label);
        builder.AppendLine();
        builder.Append(message);
        OperationLogBox.Text = builder.ToString();
        OperationLogBox.ScrollToEnd();
    }

    private static string LocalizeOperationMessage(string message)
    {
        if (!message.Any(character => character is >= '\uAC00' and <= '\uD7A3'))
        {
            if (message.Contains("runtime script is missing", StringComparison.Ordinal))
            {
                return "런타임 스크립트를 찾지 못했습니다. 런타임 폴더를 확인하세요.";
            }

            if (message.Contains("could not load the settings", StringComparison.Ordinal))
            {
                return "저장된 설정을 읽지 못했습니다.";
            }

            if (message.Contains("was canceled", StringComparison.Ordinal))
            {
                return "작업을 취소했습니다.";
            }

            if (message.Contains("timed out", StringComparison.Ordinal))
            {
                return "작업 시간이 초과되었습니다. Docker 상태를 확인하세요.";
            }

            if (message.Contains("could not start", StringComparison.Ordinal))
            {
                return "운영 작업을 시작하지 못했습니다. 런타임과 권한을 확인하세요.";
            }

            return "운영 작업에서 안전한 응답을 받지 못했습니다.";
        }

        return message;
    }

    private void BrowseRuntimeButton_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFolderDialog
        {
            Title = "Vlytics 런타임 폴더 선택",
            Multiselect = false
        };
        if (dialog.ShowDialog(this) == true)
        {
            RuntimeRootBox.Text = dialog.FolderName;
        }
    }

    private void BrowseEvidenceButton_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFileDialog
        {
            Title = "수동 검증 근거 파일 선택",
            CheckFileExists = true,
            Filter = "JSON 증거 파일 (*.json)|*.json"
        };
        if (dialog.ShowDialog(this) == true)
        {
            EvidencePathBox.Text = dialog.FileName;
        }
    }

    private void OpenBrowserButton_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var port = _savedSettings.WebPort;
            Process.Start(new ProcessStartInfo
            {
                FileName = $"http://127.0.0.1:{port}/",
                UseShellExecute = true
            });
            AppendOperationLog("브라우저", $"로컬 주소 127.0.0.1:{port}을 열었습니다.");
        }
        catch
        {
            RuntimeStateText.Text = "기본 브라우저를 열지 못했습니다.";
        }
    }

    private void SaveSnapshot(string snapshotPath)
    {
        if (!Path.IsPathFullyQualified(snapshotPath))
        {
            throw new ArgumentException("The snapshot path must be absolute.", nameof(snapshotPath));
        }

        var absolutePath = Path.GetFullPath(snapshotPath);
        var parent = Path.GetDirectoryName(absolutePath);
        if (string.IsNullOrWhiteSpace(parent))
        {
            throw new ArgumentException("The snapshot path must include a directory.", nameof(snapshotPath));
        }

        Directory.CreateDirectory(parent);
        if (Content is not FrameworkElement root)
        {
            throw new InvalidOperationException("The window content is unavailable.");
        }

        root.Measure(new Size(1180, 820));
        root.Arrange(new Rect(0, 0, 1180, 820));
        root.UpdateLayout();
        var bitmap = new RenderTargetBitmap(1180, 820, 96, 96, PixelFormats.Pbgra32);
        bitmap.Render(root);
        var encoder = new PngBitmapEncoder();
        encoder.Frames.Add(BitmapFrame.Create(bitmap));
        using var stream = File.Create(absolutePath);
        encoder.Save(stream);
    }

    private void Window_Closing(object? sender, System.ComponentModel.CancelEventArgs e)
    {
        if (_busy && !_allowClose)
        {
            e.Cancel = true;
            _closeAfterOperation = true;
            SaveStateText.Text = "진행 중인 작업을 안전하게 취소한 뒤 앱을 닫습니다.";
            _operationCancellation?.Cancel();
        }
    }
}
