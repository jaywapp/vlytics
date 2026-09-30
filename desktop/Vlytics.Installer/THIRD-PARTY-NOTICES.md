# 제3자 빌드 도구 고지

## WiX Toolset 5.0.2

Windows Installer 패키지는 WiX Toolset SDK 5.0.2로 빌드한다. 이 버전의 NuGet 패키지는 Microsoft Reciprocal License(MS-RL)로 배포된다.

- 프로젝트: https://github.com/wixtoolset/wix
- NuGet: https://www.nuget.org/packages/WixToolset.Sdk/5.0.2
- 라이선스: https://github.com/wixtoolset/wix/blob/v5.0.2/LICENSE.TXT

WiX 6부터 도입된 Open Source Maintenance Fee는 WiX 5.0.2에는 적용되지 않는다. WiX는 빌드 시에만 사용하며, WiX 실행 파일이나 SDK를 Vlytics MSI에 포함하지 않는다.

## .NET 10 Windows Desktop Runtime

Vlytics 설정 앱은 Microsoft .NET 10 Windows Desktop Runtime을 자체 포함 배포한다. .NET Runtime은 MIT License로 배포되며 Microsoft와 각 제3자 구성 요소의 고지가 적용된다.

- 프로젝트와 라이선스: https://github.com/dotnet/runtime
- 제3자 고지: https://github.com/dotnet/runtime/blob/main/THIRD-PARTY-NOTICES.TXT
