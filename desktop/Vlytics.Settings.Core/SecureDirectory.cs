using System.Security.AccessControl;
using System.Security.Principal;

namespace Vlytics.Settings.Core;

internal static class SecureDirectory
{
    public static string CreateForCurrentUser(string path)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(path);

        var fullPath = Path.GetFullPath(path);
        Directory.CreateDirectory(fullPath);

        var currentUser = WindowsIdentity.GetCurrent().User
            ?? throw new InvalidOperationException("The current Windows user could not be identified.");
        var security = new DirectorySecurity();
        security.SetOwner(currentUser);
        security.SetAccessRuleProtection(isProtected: true, preserveInheritance: false);
        security.AddAccessRule(new FileSystemAccessRule(
            currentUser,
            FileSystemRights.FullControl,
            InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
            PropagationFlags.None,
            AccessControlType.Allow));

        new DirectoryInfo(fullPath).SetAccessControl(security);
        return fullPath;
    }
}
