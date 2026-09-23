# Restore BrownDust II after MaaFramework leaves it transparent/click-through.
# No Python, MaaFramework or third-party PowerShell modules required.
# Use -CheckOnly to inspect without changing the window.
[CmdletBinding()]
param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'

try {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class BD2WindowRecovery
{
    private const int GWL_EXSTYLE = -20;
    private const int WS_EX_LAYERED = 0x80000;
    private const int WS_EX_TRANSPARENT = 0x20;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr FindWindow(string className, string title);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern int GetWindowLongW(IntPtr hwnd, int index);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern int SetWindowLongW(IntPtr hwnd, int index, int value);
    [DllImport("kernel32.dll")]
    private static extern void SetLastError(uint error);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetLayeredWindowAttributes(IntPtr hwnd, uint color, byte alpha, uint flags);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool GetLayeredWindowAttributes(IntPtr hwnd, out uint color, out byte alpha, out uint flags);
    [DllImport("user32.dll")]
    private static extern bool ShowWindowAsync(IntPtr hwnd, int command);
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")]
    private static extern bool IsWindow(IntPtr hwnd);

    public sealed class State
    {
        public string Handle;
        public string ExtendedStyle;
        public bool Layered;
        public int? Alpha;
        public uint? LayerFlags;
        public bool ClickThrough;
        public bool Minimized;
        public bool Visible;
        public bool Foreground;
        public bool Recovered;
    }

    private static int ReadStyle(IntPtr hwnd)
    {
        if (!IsWindow(hwnd)) throw new InvalidOperationException("Game window closed. Run again after opening the game.");
        SetLastError(0);
        int style = GetWindowLongW(hwnd, GWL_EXSTYLE);
        int error = Marshal.GetLastWin32Error();
        if (style == 0 && error != 0) throw new Win32Exception(error);
        return style;
    }

    public static State Inspect(IntPtr hwnd)
    {
        int style = ReadStyle(hwnd);
        var state = new State();
        state.Handle = "0x" + hwnd.ToInt64().ToString("X");
        state.ExtendedStyle = "0x" + style.ToString("X8");
        state.Layered = (style & WS_EX_LAYERED) != 0;
        if (state.Layered) {
            uint color, flags;
            byte alpha;
            if (GetLayeredWindowAttributes(hwnd, out color, out alpha, out flags)) {
                state.Alpha = alpha;
                state.LayerFlags = flags;
            }
        }
        state.ClickThrough = (style & WS_EX_TRANSPARENT) != 0;
        state.Minimized = IsIconic(hwnd);
        state.Visible = IsWindowVisible(hwnd);
        state.Foreground = GetForegroundWindow() == hwnd;
        state.Recovered = state.Visible && !state.Minimized && !state.ClickThrough
            && (!state.Layered || (state.Alpha == 255 && state.LayerFlags == 2));
        return state;
    }

    public static void Restore(IntPtr hwnd)
    {
        int style = ReadStyle(hwnd);
        if ((style & WS_EX_LAYERED) != 0 && !SetLayeredWindowAttributes(hwnd, 0, 255, 2))
            throw new Win32Exception(Marshal.GetLastWin32Error());

        // Read again in case the framework updated the style in the meantime.
        style = ReadStyle(hwnd);
        if ((style & WS_EX_TRANSPARENT) != 0) {
            SetLastError(0);
            int previous = SetWindowLongW(hwnd, GWL_EXSTYLE, style & ~WS_EX_TRANSPARENT);
            int error = Marshal.GetLastWin32Error();
            if (previous == 0 && error != 0) throw new Win32Exception(error);
        }
        // Preserve maximized/normal geometry; restore only minimized/hidden windows.
        if (IsIconic(hwnd) || !IsWindowVisible(hwnd)) ShowWindowAsync(hwnd, 9);
        SetForegroundWindow(hwnd);
    }
}
'@

    $gameWindow = [BD2WindowRecovery]::FindWindow('UnityWndClass', 'BrownDust II')
    if ($gameWindow -eq [IntPtr]::Zero) {
        throw '未找到 BrownDust II 游戏窗口，请先打开游戏。'
    }
    Write-Host '当前窗口状态：'
    [BD2WindowRecovery]::Inspect($gameWindow) | Format-List | Out-Host
    if ($CheckOnly) { exit 0 }

    [BD2WindowRecovery]::Restore($gameWindow)
    Start-Sleep -Milliseconds 600
    $state = [BD2WindowRecovery]::Inspect($gameWindow)
    # Activation may make an existing controller restore its stale alpha value.
    # Repair once more after that callback, then verify without an endless loop.
    if (-not $state.Recovered) {
        [BD2WindowRecovery]::Restore($gameWindow)
    }
    Start-Sleep -Milliseconds 1200
    $state = [BD2WindowRecovery]::Inspect($gameWindow)
    if (-not $state.Recovered) {
        $state | Format-List | Out-Host
        throw '窗口仍未恢复，可能有控制器持续修改窗口。请停止自动化任务后重试。'
    }
    Write-Host '已恢复：窗口可见、完全不透明、取消点击穿透。' -ForegroundColor Green
    if (-not $state.Foreground) {
        Write-Host '系统未允许切换到前台；窗口状态已恢复，可点击任务栏打开。'
    }
    exit 0
}
catch {
    Write-Host ('恢复失败：' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '若提示拒绝访问，请右键 .cmd 文件，选择“以管理员身份运行”。'
    exit 1
}
