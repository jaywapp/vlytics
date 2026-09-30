namespace Vlytics.Settings.Core;

public sealed record AppSettings
{
    public string RuntimeRoot { get; set; } = Path.GetFullPath(
        Path.Combine(AppContext.BaseDirectory, "..", "runtime"));

    public string Provider { get; set; } = "openai";

    public string ModelId { get; set; } = "gpt-6-sol";

    public string PinnedModelVersion { get; set; } = string.Empty;

    public bool ModelVerified { get; set; }

    public decimal PricingToBudgetRate { get; set; }

    public decimal InputPrice { get; set; } = 2.50m;

    public decimal OutputPrice { get; set; } = 10m;

    public decimal DailyBudget { get; set; } = 1000m;

    public decimal MonthlyBudget { get; set; } = 10000m;

    public int MaxCallsPerMatch { get; set; } = 3;

    public int DailyCallLimit { get; set; } = 100;

    public int MonthlyCallLimit { get; set; } = 1000;

    public int MaxInputTokens { get; set; } = 4000;

    public int MaxOutputTokens { get; set; } = 1000;

    public int WebPort { get; set; } = 8080;

    public string DryRunEvidencePath { get; set; } = string.Empty;

    public string SshHost { get; set; } = string.Empty;

    public string SshUser { get; set; } = string.Empty;

    public int SshPort { get; set; } = 22;

    public Dictionary<string, string> Images { get; set; } = CreateDefaultImages();

    internal static IReadOnlyList<string> ImageNames { get; } =
    [
        "VLYTICS_BACKEND_IMAGE",
        "VLYTICS_FRONTEND_IMAGE",
        "VLYTICS_NODE_BUILD_IMAGE",
        "VLYTICS_NGINX_RUNTIME_IMAGE",
        "VLYTICS_POSTGRES_IMAGE",
        "VLYTICS_PYTHON_BUILD_IMAGE",
        "VLYTICS_UV_BUILD_IMAGE",
    ];

    private static Dictionary<string, string> CreateDefaultImages()
    {
        return ImageNames.ToDictionary(name => name, _ => string.Empty, StringComparer.Ordinal);
    }
}
