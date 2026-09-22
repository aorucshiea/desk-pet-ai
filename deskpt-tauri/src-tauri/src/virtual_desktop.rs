//! virtual_desktop — Windows virtual desktop support for the pet body.
//!
//! Two jobs:
//! 1. Fix "pet appears on EVERY virtual desktop" (topmost windows are
//!    shown on all desktops by Windows): poll IsWindowOnCurrentVirtualDesktop
//!    and HIDE the pet when it is not on the current desktop.
//! 2. [WALK_DESKTOP]: move the pet to another desktop. IVirtualDesktopManager
//!    only exposes MoveWindowToDesktop(hwnd, guid) — the target GUID is
//!    scavenged from any other visible titled window living on a different
//!    desktop (documented APIs only).

#![cfg(windows)]

use windows::core::{BOOL, GUID};
use windows::Win32::Foundation::{HWND, LPARAM};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CLSCTX_ALL, COINIT_APARTMENTTHREADED,
};
use windows::Win32::UI::Shell::{IVirtualDesktopManager, VirtualDesktopManager};
use windows::Win32::UI::WindowsAndMessaging::{
    EnumWindows, GetWindowTextLengthW, IsWindowVisible,
};

pub fn ensure_com() {
    unsafe {
        let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
    }
}

fn hwnd_from_isize(h: isize) -> HWND {
    HWND(h as *mut core::ffi::c_void)
}

fn manager() -> windows::core::Result<IVirtualDesktopManager> {
    unsafe { CoCreateInstance(&VirtualDesktopManager, None, CLSCTX_ALL) }
}

/// Is this window on the CURRENT virtual desktop? None = the API refused
/// (some windows are unsupported) — the caller keeps current behavior.
pub fn is_on_current_desktop(hwnd: isize) -> Option<bool> {
    let m = manager().ok()?;
    let on = unsafe { m.IsWindowOnCurrentVirtualDesktop(hwnd_from_isize(hwnd)) }.ok()?;
    Some(on.as_bool())
}

fn window_desktop_guid(hwnd: isize) -> Option<GUID> {
    let m = manager().ok()?;
    unsafe { m.GetWindowDesktopId(hwnd_from_isize(hwnd)) }.ok()
}

fn move_window_to_desktop(hwnd: isize, guid: &GUID) -> bool {
    match manager() {
        Ok(m) => unsafe {
            m.MoveWindowToDesktop(hwnd_from_isize(hwnd), guid).is_ok()
        },
        Err(_) => false,
    }
}

// EnumWindows callbacks cannot capture — stash scan results here.
static MY_DESKTOP: std::sync::Mutex<Option<GUID>> = std::sync::Mutex::new(None);
static FOUND_OTHER: std::sync::Mutex<Option<GUID>> = std::sync::Mutex::new(None);

unsafe extern "system" fn enum_find_other(hwnd: HWND, _l: LPARAM) -> BOOL {
    if !IsWindowVisible(hwnd).as_bool() || GetWindowTextLengthW(hwnd) == 0 {
        return BOOL(1);
    }
    if let Some(g) = window_desktop_guid(hwnd.0 as isize) {
        let mine = *MY_DESKTOP.lock().unwrap();
        if let Some(mine) = mine {
            if g != mine {
                *FOUND_OTHER.lock().unwrap() = Some(g);
                return BOOL(0); // stop scanning
            }
        }
    }
    BOOL(1)
}

/// Hop the given windows to another virtual desktop. Returns true when
/// at least one window moved (false = single desktop / API refused).
pub fn hop_to_other_desktop(hwnds: &[isize]) -> bool {
    ensure_com();
    let Some(first) = hwnds.first().copied() else {
        return false;
    };
    let Some(my) = window_desktop_guid(first) else {
        return false;
    };
    *MY_DESKTOP.lock().unwrap() = Some(my);
    *FOUND_OTHER.lock().unwrap() = None;
    unsafe {
        let _ = EnumWindows(Some(enum_find_other), LPARAM(0));
    }
    let Some(target) = *FOUND_OTHER.lock().unwrap() else {
        return false;
    };
    hwnds.iter().any(|h| move_window_to_desktop(*h, &target))
}
