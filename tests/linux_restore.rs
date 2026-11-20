//! A real, single-threaded Linux PTY survives a same-PID exec and is reaped once.
//! This fixture has no libtest worker threads before calling unsafe forkpty.

#[cfg(not(target_os = "linux"))]
fn main() {}

#[cfg(target_os = "linux")]
fn main() {
    use std::os::unix::process::CommandExt;
    use std::process::Command;
    use std::time::{Duration, Instant};
    use zpty::{Pty, Size};

    unsafe extern "C" {
        fn fcntl(fd: i32, command: i32, ...) -> i32;
    }

    fn until(pty: &Pty, marker: &[u8]) -> Vec<u8> {
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut output = Vec::new();
        while Instant::now() < deadline {
            let mut bytes = [0u8; 512];
            match pty.read(&mut bytes) {
                Ok(0) => break,
                Ok(count) => {
                    output.extend_from_slice(&bytes[..count]);
                    if output.windows(marker.len()).any(|part| part == marker) {
                        return output;
                    }
                }
                Err(error) if error.is_would_block() => {
                    std::thread::sleep(Duration::from_millis(1));
                }
                Err(error) => panic!("PTY read failed: {error}"),
            }
        }
        panic!("PTY marker {marker:?} was not read: {output:?}");
    }

    let arguments: Vec<String> = std::env::args().collect();
    if arguments.get(1).is_some_and(|value| value == "--restored") {
        assert_eq!(arguments.len(), 5);
        let fd = arguments[2].parse::<i32>().unwrap();
        let child = arguments[3].parse::<i32>().unwrap();
        let parent = arguments[4].parse::<u32>().unwrap();
        assert_eq!(std::process::id(), parent, "exec changed the parent PID");
        let flags = unsafe { fcntl(fd, 1) };
        assert!(flags >= 0 && flags & 1 == 0, "master was not inherited");
        assert_eq!(unsafe { fcntl(fd, 2, flags | 1) }, 0);

        // The previous process image had the sole owner; exec did not run Drop.
        let mut pty = unsafe { Pty::from_raw_parts(fd, child, None) };
        assert_eq!((pty.fd(), pty.pid(), pty.status()), (fd, child, None));
        assert_eq!(pty.reap().unwrap(), None, "child was replaced or exited");
        let input = b"continued-after-exec\n";
        let mut sent = 0;
        while sent < input.len() {
            match pty.write(&input[sent..]) {
                Ok(0) => panic!("PTY write made no progress"),
                Ok(count) => sent += count,
                Err(error) if error.is_would_block() => {
                    std::thread::sleep(Duration::from_millis(1));
                }
                Err(error) => panic!("PTY write failed: {error}"),
            }
        }
        until(&pty, b"reply:continued-after-exec");
        let status = pty.wait_timeout(5_000).unwrap().expect("child exit");
        assert_eq!(status.code(), Some(7));
        let raw = status.raw();
        std::mem::forget(pty);

        // Transferring an already reaped owner must not call waitpid again or
        // signal a possibly reused PID when dropped.
        let mut cached = unsafe { Pty::from_raw_parts(fd, child, Some(raw)) };
        assert_eq!(cached.status(), Some(status));
        assert_eq!(cached.reap().unwrap(), Some(status));
        assert_eq!(cached.wait().unwrap(), status);
        assert_eq!(unsafe { fcntl(fd, 1) } & 1, 1);
        drop(cached);
        assert_eq!(unsafe { fcntl(fd, 1) }, -1, "Drop must close the master");
        println!(
            "{{\"suite\":\"linux-pty-exec-restore\",\"passed\":true,\"same_parent_pid\":true,\"child_exit_code\":7}}"
        );
        return;
    }
    assert_eq!(arguments.len(), 1, "unexpected fixture arguments");
    let parent = std::process::id();
    // This standalone fixture has not spawned threads; arguments/environment
    // stay alive, and no other owner reaps or closes the child/master.
    let pty = unsafe {
        Pty::spawn(
            c"/bin/sh",
            &[
                c"sh",
                c"-c",
                c"printf 'ready-before-exec\n'; IFS= read -r value; printf 'reply:%s\n' \"$value\"; exit 7",
            ],
            &[c"PATH=/usr/bin:/bin", c"LC_ALL=C"],
            Size::new(80, 24),
            &[],
        )
    }
    .unwrap();
    until(&pty, b"ready-before-exec");
    let flags = unsafe { fcntl(pty.fd(), 1) };
    assert!(flags >= 0 && flags & 1 != 0);
    assert_eq!(unsafe { fcntl(pty.fd(), 2, flags & !1) }, 0);
    let error = Command::new(std::env::current_exe().unwrap())
        .arg("--restored")
        .arg(pty.fd().to_string())
        .arg(pty.pid().to_string())
        .arg(parent.to_string())
        .exec();
    // exec failed: the original object is still the sole owner and cleans up.
    assert_eq!(unsafe { fcntl(pty.fd(), 2, flags) }, 0);
    panic!("fixture exec failed: {error}");
}
