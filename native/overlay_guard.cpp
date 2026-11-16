// overlay-guard.exe: starts the overlay and puts it on one mouse button.
//
//   press             overlay running -> end it and every process it started
//                     overlay stopped -> start it again
//   Ctrl + press      end the overlay and this program
//
// The overlay runs inside a job object of this program, so everything it starts
// (Claude Code, node, their children) is in the job too and dies with it. The
// button is watched by a low-level mouse hook here, outside Python: it works
// even when the overlay hangs. The press is swallowed, so a side button does
// not also go "back"/"forward" in a browser.
//
// Usage: overlay-guard.exe [mouse3|mouse4|mouse5]   (default mouse5)
//   mouse3 = wheel click, mouse4 = side "back", mouse5 = side "forward".
// Started again while running, it just starts the overlay if it is stopped.
//
// Build: native\build.bat (MinGW g++); the exe goes next to overlay.py.
#include <windows.h>
#include <shellapi.h>
#include <wchar.h>

namespace {

constexpr UINT WM_GUARD_PRESS = WM_APP + 1;  // wParam: 1 if Ctrl was held
const wchar_t* const kTitle = L"AI overlay guard";

DWORD g_main_thread = 0;
int g_button = 5;
HANDLE g_job = nullptr;
wchar_t g_dir[MAX_PATH];

int button_of(WPARAM message, const MSLLHOOKSTRUCT* info) {
    switch (message) {
    case WM_MBUTTONDOWN:
    case WM_MBUTTONUP:
        return 3;
    case WM_XBUTTONDOWN:
    case WM_XBUTTONUP:
        return HIWORD(info->mouseData) == XBUTTON1 ? 4 : 5;
    default:
        return 0;
    }
}

LRESULT CALLBACK mouse_hook(int code, WPARAM wparam, LPARAM lparam) {
    if (code == HC_ACTION) {
        auto* info = reinterpret_cast<MSLLHOOKSTRUCT*>(lparam);
#ifdef GUARD_TEST
        bool real = true;  // test build: simulated clicks count too
#else
        bool real = !(info->flags & LLMHF_INJECTED);  // clicks sent by programs do not
#endif
        if (real && button_of(wparam, info) == g_button) {
            if (wparam == WM_MBUTTONDOWN || wparam == WM_XBUTTONDOWN) {
                bool ctrl = GetAsyncKeyState(VK_CONTROL) & 0x8000;
                // The work happens in the message loop: a hook must return at once.
                PostThreadMessageW(g_main_thread, WM_GUARD_PRESS, ctrl ? 1 : 0, 0);
            }
            return 1;  // swallow the press and its release
        }
    }
    return CallNextHookEx(nullptr, code, wparam, lparam);
}

bool overlay_running() {
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION info = {};
    return QueryInformationJobObject(g_job, JobObjectBasicAccountingInformation,
                                     &info, sizeof info, nullptr)
        && info.ActiveProcesses > 0;
}

void stop_overlay() {
    TerminateJobObject(g_job, 1);
}

void start_overlay() {
    wchar_t exe[MAX_PATH + 64];
    swprintf(exe, sizeof exe / sizeof *exe, L"%ls\\.venv\\Scripts\\pythonw.exe", g_dir);
    wchar_t cmd[2 * MAX_PATH];
    swprintf(cmd, sizeof cmd / sizeof *cmd, L"\"%ls\" overlay.py", exe);
    STARTUPINFOW si = {};
    si.cb = sizeof si;
    PROCESS_INFORMATION pi = {};
    // Suspended until it is in the job, so not even its first child escapes.
    if (!CreateProcessW(exe, cmd, nullptr, nullptr, FALSE, CREATE_SUSPENDED,
                        nullptr, g_dir, &si, &pi)) {
        MessageBoxW(nullptr, L"Не вдалося запустити оверлей. Спершу запустіть run.bat, "
                             L"щоб він встановив залежності.", kTitle, MB_ICONERROR);
        return;
    }
    if (!AssignProcessToJobObject(g_job, pi.hProcess)) {
        TerminateProcess(pi.hProcess, 1);
        MessageBoxW(nullptr, L"Windows не дала взяти оверлей під контроль (job object).",
                    kTitle, MB_ICONERROR);
    } else {
        ResumeThread(pi.hThread);
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
}

int parse_button(const wchar_t* text) {
    if (!wcscmp(text, L"mouse3")) return 3;
    if (!wcscmp(text, L"mouse4")) return 4;
    if (!wcscmp(text, L"mouse5")) return 5;
    return 0;
}

}  // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int) {
    int argc = 0;
    wchar_t** argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (argv && argc > 1) {
        g_button = parse_button(argv[1]);
        if (!g_button) {
            MessageBoxW(nullptr, L"Кнопка: mouse3 (колесо), mouse4 або mouse5 (бічні).",
                        kTitle, MB_ICONERROR);
            return 1;
        }
    }
    LocalFree(argv);

    // A second start only asks the running guard to start the overlay.
    HANDLE wake = CreateEventW(nullptr, FALSE, FALSE, L"Local\\ai-overlay-guard-start");
    CreateMutexW(nullptr, FALSE, L"Local\\ai-overlay-guard");
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        SetEvent(wake);
        return 0;
    }

    GetModuleFileNameW(nullptr, g_dir, MAX_PATH);
    if (wchar_t* slash = wcsrchr(g_dir, L'\\')) *slash = 0;

    g_job = CreateJobObjectW(nullptr, nullptr);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {};
    // If the guard itself dies, Windows ends the overlay too: nothing is left behind.
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    SetInformationJobObject(g_job, JobObjectExtendedLimitInformation, &limits, sizeof limits);

    g_main_thread = GetCurrentThreadId();
    HHOOK hook = SetWindowsHookExW(WH_MOUSE_LL, mouse_hook, instance, 0);
    if (!hook) {
        MessageBoxW(nullptr, L"Не вдалося стежити за мишею (hook).", kTitle, MB_ICONERROR);
        return 1;
    }

    start_overlay();
    for (;;) {
        // The low-level hook is called through this loop, so it must keep running.
        DWORD woke = MsgWaitForMultipleObjects(1, &wake, FALSE, INFINITE, QS_ALLINPUT);
        if (woke == WAIT_OBJECT_0) {
            if (!overlay_running()) start_overlay();
            continue;
        }
        MSG msg;
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
            if (msg.message == WM_QUIT) goto done;
            if (msg.message == WM_GUARD_PRESS) {
                if (msg.wParam) {  // Ctrl + button: everything off, guard too
                    stop_overlay();
                    goto done;
                }
                if (overlay_running()) stop_overlay();
                else start_overlay();
            }
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
done:
    UnhookWindowsHookEx(hook);
    return 0;
}
