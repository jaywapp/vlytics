function Assert-HomeProviderEgress {
    param(
        [object]$Rendered,
        [string[]]$EnabledProviders = @("openai"),
        [switch]$AllowExampleKeys
    )

    $routes = @{
        openai = @{ Service = "openai_egress"; Network = "openai_access"; Env = "VLYTICS_OPENAI_PROXY_URL"; Key = "VLYTICS_OPENAI_API_KEY" }
        anthropic = @{ Service = "anthropic_egress"; Network = "anthropic_access"; Env = "VLYTICS_ANTHROPIC_PROXY_URL"; Key = "VLYTICS_ANTHROPIC_API_KEY" }
        google = @{ Service = "google_egress"; Network = "google_access"; Env = "VLYTICS_GOOGLE_PROXY_URL"; Key = "VLYTICS_GOOGLE_API_KEY" }
    }
    if ($EnabledProviders.Count -eq 0) { throw "Home deployment requires an enabled Provider." }
    foreach ($provider in $EnabledProviders) {
        if (-not $routes.ContainsKey($provider)) { throw "Home deployment contains an unknown Provider." }
    }
    $worker = $Rendered.services.worker
    $workerNetworks = @($worker.networks.PSObject.Properties.Name)
    if ($workerNetworks.Count -ne 1 -or $workerNetworks[0] -ne "private" -or
        $Rendered.networks.private.internal -ne $true) {
        throw "Home worker must remain on the internal private network."
    }
    foreach ($provider in $routes.Keys) {
        $route = $routes[$provider]
        $relayProperty = $Rendered.services.PSObject.Properties[$route.Service]
        $proxyProperty = $worker.environment.PSObject.Properties[$route.Env]
        $keyProperty = $worker.environment.PSObject.Properties[$route.Key]
        if ($provider -notin $EnabledProviders) {
            if ($null -ne $relayProperty -or $null -ne $proxyProperty -or $null -ne $keyProperty) {
                throw "Disabled Provider must not have a relay, proxy or key in the home worker."
            }
            continue
        }
        if ($null -eq $relayProperty) { throw "Home Provider egress is not connected." }
        if ($null -eq $keyProperty -or [string]::IsNullOrWhiteSpace([string]$keyProperty.Value) -or
            (-not $AllowExampleKeys -and [string]$keyProperty.Value -match '^__.*__$')) {
            throw "Enabled Provider requires a resolved key in the home worker."
        }
        if ($null -eq $proxyProperty -or $proxyProperty.Value -ne "http://$($route.Service):8081") {
            throw "Home Provider proxy must use its reviewed internal endpoint."
        }
        $relay = $relayProperty.Value
        $dependency = $worker.depends_on.PSObject.Properties[$route.Service]
        $disabledHealth = $relay.healthcheck.PSObject.Properties["disable"]
        if ($null -eq $dependency -or $dependency.Value.condition -ne "service_healthy" -or
            ($null -ne $disabledHealth -and $disabledHealth.Value -eq $true) -or @($relay.healthcheck.test).Count -eq 0 -or
            $relay.healthcheck.test[0] -eq "NONE") {
            throw "Home worker must wait for its Provider relay health check."
        }
        $relayNetworks = @($relay.networks.PSObject.Properties.Name)
        if ($relayNetworks.Count -ne 2 -or "private" -notin $relayNetworks -or $route.Network -notin $relayNetworks -or
            $Rendered.networks.PSObject.Properties[$route.Network].Value.driver -ne "bridge") {
            throw "Home Provider egress network isolation is invalid."
        }
        $accessPriority = $relay.networks.PSObject.Properties[$route.Network].Value.PSObject.Properties["gw_priority"]
        $privatePriority = $relay.networks.private.PSObject.Properties["gw_priority"]
        if ($null -eq $accessPriority -or $accessPriority.Value -ne 1 -or
            ($null -ne $privatePriority -and $privatePriority.Value -gt 0)) {
            throw "Home Provider relay must use its access network as the default gateway."
        }
        $portsProperty = $relay.PSObject.Properties["ports"]
        if ($null -ne $portsProperty -and @($portsProperty.Value).Count -gt 0) {
            throw "Home Provider relay must not publish a host port."
        }
        $volumesProperty = $relay.PSObject.Properties["volumes"]
        if ($null -ne $volumesProperty -and @($volumesProperty.Value).Count -gt 0) {
            throw "Home Provider relay must not mount application files or credentials."
        }
        if (($relay.command -join " ") -ne "python -m vlytics.ops.provider_egress" -or
            $relay.environment.VLYTICS_EGRESS_BIND -ne "0.0.0.0:8081" -or
            $relay.environment.VLYTICS_EGRESS_PROVIDER -ne $provider -or
            $relay.image -ne $worker.image) {
            throw "Home Provider relay must run the reviewed module and selected Provider."
        }
        $allowedEnvironment = @("VLYTICS_EGRESS_BIND", "VLYTICS_EGRESS_PROVIDER", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED")
        foreach ($entry in $relay.environment.PSObject.Properties) {
            if ($entry.Name -notin $allowedEnvironment) {
                throw "Home Provider relay environment must not receive credentials or other settings."
            }
        }
        foreach ($serviceProperty in $Rendered.services.PSObject.Properties) {
            if ($serviceProperty.Name -ne $route.Service -and
                $route.Network -in @($serviceProperty.Value.networks.PSObject.Properties.Name)) {
                throw "Only the selected Provider relay may join its access network."
            }
        }
    }
}
