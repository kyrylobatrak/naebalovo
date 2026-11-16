// overlay-guard for macOS: starts the overlay and puts it on one mouse button.
// The same behaviour as native/overlay_guard.cpp on Windows:
//
//   press             overlay running -> end it and every process it started
//                     overlay stopped -> start it again
//   Ctrl + press      end the overlay and this program
//
// The overlay starts as the leader of a process group of its own; whatever it
// starts (Claude Code, node, their children) inherits the group, so killing the
// group ends them all. Descendants that left the group (setsid) are found
// through the process tree and killed too. The button is watched by a Quartz
// event tap here, outside Python: it works even when the overlay hangs. The
// press is swallowed, so a side button does not also go back/forward in a browser.
//
// Usage (from the project folder, as mac/run.command does):
//   mac/overlay-guard [mouse3|mouse4|mouse5]      (default mouse5)
//   mouse3 = wheel click, mouse4 = side "back", mouse5 = side "forward".
// Started again while running, it just starts the overlay if it is stopped.
// Needs the Accessibility permission of the app that starts it (e.g. Terminal).
//
// Build (run.command does it on the first start):
//   clang++ -std=c++17 -O2 -o mac/overlay-guard mac/overlay_guard.cpp \
//       -framework ApplicationServices -framework CoreFoundation
#include <ApplicationServices/ApplicationServices.h>
#include <CoreFoundation/CoreFoundation.h>
#include <dispatch/dispatch.h>
#include <fcntl.h>
#include <libproc.h>
#include <signal.h>
#include <spawn.h>
#include <sys/file.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

extern char** environ;

namespace {

const char* const kLockFile = "/tmp/ai-overlay-guard.lock";  // holds the guard's pid
const char* const kPython = ".venv-mac/bin/python";

int g_button = 4;          // CGEvent button number: 2 wheel, 3 back, 4 forward
pid_t g_overlay = 0;       // leader of the overlay's process group; 0 = not started
CFMachPortRef g_tap = nullptr;

void alert(const char* text) {
    CFStringRef message = CFStringCreateWithCString(nullptr, text, kCFStringEncodingUTF8);
    CFUserNotificationDisplayAlert(0, kCFUserNotificationStopAlertLevel, nullptr, nullptr,
                                   nullptr, CFSTR("AI overlay guard"), message,
                                   nullptr, nullptr, nullptr, nullptr);
    CFRelease(message);
}

void reap() {
    // Exited children stay zombies (and keep the group "alive") until waited for.
    while (waitpid(-1, nullptr, WNOHANG) > 0) {
    }
}

bool overlay_running() {
    reap();
    return g_overlay > 0 && kill(-g_overlay, 0) == 0;
}

void collect_descendants(pid_t pid, std::vector<pid_t>& out) {
    std::vector<pid_t> children(256);
    int count = proc_listchildpids(pid, children.data(),
                                   static_cast<int>(children.size() * sizeof(pid_t)));
    if (count <= 0) return;
    // Older macOS returns bytes, newer returns a count of pids.
    if (count > static_cast<int>(children.size())) count /= static_cast<int>(sizeof(pid_t));
    for (int i = 0; i < count && i < static_cast<int>(children.size()); ++i) {
        if (children[i] <= 0) continue;
        out.push_back(children[i]);
        collect_descendants(children[i], out);
    }
}

void stop_overlay() {
    if (g_overlay <= 0) return;
    std::vector<pid_t> tree;
    collect_descendants(g_overlay, tree);  // before the kill: orphans lose their parent
    kill(-g_overlay, SIGKILL);             // the whole group, the overlay included
    for (pid_t pid : tree) kill(pid, SIGKILL);  // those that left the group
    reap();
}

void start_overlay() {
    if (access(kPython, X_OK) != 0) {
        alert("Не знайдено .venv-mac. Спершу запустіть mac/run.command, "
              "щоб він встановив залежності.");
        return;
    }
    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGDEF
                                    | POSIX_SPAWN_SETSIGMASK);
    posix_spawnattr_setpgroup(&attr, 0);  // a new group, led by the overlay
    sigset_t signals;
    sigemptyset(&signals);
    posix_spawnattr_setsigmask(&attr, &signals);  // the guard blocks nothing it should pass on
    sigaddset(&signals, SIGUSR1);
    sigaddset(&signals, SIGTERM);
    sigaddset(&signals, SIGINT);
    // Ignored here, default there. SIGHUP stays ignored, as with nohup before:
    // closing the Terminal window must not end the overlay.
    posix_spawnattr_setsigdefault(&attr, &signals);
    char* argv[] = {const_cast<char*>(kPython), const_cast<char*>("overlay.py"), nullptr};
    pid_t pid = 0;
    int error = posix_spawn(&pid, kPython, nullptr, &attr, argv, environ);
    posix_spawnattr_destroy(&attr);
    if (error) {
        alert("Не вдалося запустити оверлей.");
        return;
    }
    g_overlay = pid;
}

void on_press(bool ctrl) {
    if (ctrl) {  // Ctrl + button: everything off, guard too
        stop_overlay();
        unlink(kLockFile);
        exit(0);
    }
    if (overlay_running()) stop_overlay();
    else start_overlay();
}

CGEventRef tap_callback(CGEventTapProxy, CGEventType type, CGEventRef event, void*) {
    if (type == kCGEventTapDisabledByTimeout || type == kCGEventTapDisabledByUserInput) {
        if (g_tap) CGEventTapEnable(g_tap, true);  // macOS turns a slow tap off: turn it back on
        return event;
    }
    if (CGEventGetIntegerValueField(event, kCGMouseEventButtonNumber) != g_button) return event;
    if (type == kCGEventOtherMouseDown) {
        bool ctrl = CGEventGetFlags(event) & kCGEventFlagMaskControl;
        // The work happens after the callback returns: a tap must answer at once.
        dispatch_async(dispatch_get_main_queue(), ^{ on_press(ctrl); });
    }
    return nullptr;  // swallow the press and its release
}

// One guard at a time: the lock file is held while it runs and keeps its pid.
// Returns false if another guard runs; that one is then asked to start the overlay.
bool single_instance() {
    int fd = open(kLockFile, O_RDWR | O_CREAT, 0644);
    if (fd < 0) return true;
    if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
        char text[32] = {};
        if (read(fd, text, sizeof text - 1) > 0) {
            pid_t other = static_cast<pid_t>(atoi(text));
            if (other > 0) kill(other, SIGUSR1);
        }
        close(fd);
        return false;
    }
    ftruncate(fd, 0);
    dprintf(fd, "%d\n", getpid());
    return true;  // fd stays open: the lock lives as long as the guard
}

void on_signal(int number, void (^handler)()) {
    signal(number, SIG_IGN);  // delivered through the dispatch source instead
    dispatch_source_t source = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, number, 0,
                                                      dispatch_get_main_queue());
    dispatch_source_set_event_handler(source, handler);
    dispatch_resume(source);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc > 1) {
        std::string name = argv[1];
        if (name == "mouse3") g_button = 2;
        else if (name == "mouse4") g_button = 3;
        else if (name == "mouse5") g_button = 4;
        else {
            fprintf(stderr, "button: mouse3 (wheel), mouse4 or mouse5 (side)\n");
            return 1;
        }
    }
    if (!single_instance()) return 0;

    on_signal(SIGUSR1, ^{ if (!overlay_running()) start_overlay(); });  // a second start
    auto quit = ^{ stop_overlay(); unlink(kLockFile); exit(0); };
    on_signal(SIGTERM, quit);
    on_signal(SIGINT, quit);
    signal(SIGHUP, SIG_IGN);  // the Terminal window may be closed
    // A quiet check now and then: zombies of the overlay are reaped.
    dispatch_source_t timer = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0,
                                                     dispatch_get_main_queue());
    dispatch_source_set_timer(timer, dispatch_time(DISPATCH_TIME_NOW, 0), 5 * NSEC_PER_SEC,
                              NSEC_PER_SEC);
    dispatch_source_set_event_handler(timer, ^{ reap(); });
    dispatch_resume(timer);

    CGEventMask mask = CGEventMaskBit(kCGEventOtherMouseDown) | CGEventMaskBit(kCGEventOtherMouseUp);
    g_tap = CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap, kCGEventTapOptionDefault,
                             mask, tap_callback, nullptr);
    if (!g_tap) {
        const void* keys[] = {kAXTrustedCheckOptionPrompt};
        const void* values[] = {kCFBooleanTrue};
        CFDictionaryRef options = CFDictionaryCreate(nullptr, keys, values, 1,
                                                     &kCFTypeDictionaryKeyCallBacks,
                                                     &kCFTypeDictionaryValueCallBacks);
        AXIsProcessTrustedWithOptions(options);  // opens the permission prompt
        CFRelease(options);
        alert("Кнопка миші не працюватиме: потрібен дозвіл «Універсальний доступ» "
              "(Accessibility) для Terminal. Дайте його і запустіть run.command знову.");
        start_overlay();  // the overlay still works, just without the button
        unlink(kLockFile);
        return 1;
    }
    CFRunLoopSourceRef source = CFMachPortCreateRunLoopSource(nullptr, g_tap, 0);
    CFRunLoopAddSource(CFRunLoopGetMain(), source, kCFRunLoopCommonModes);
    CGEventTapEnable(g_tap, true);

    start_overlay();
    CFRunLoopRun();  // also runs the main dispatch queue (presses, signals, timer)
    return 0;
}
