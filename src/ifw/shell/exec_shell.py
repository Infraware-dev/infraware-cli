#!/usr/bin/env python3
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import tty
import threading
import time
from typing import Optional, Callable
import struct
import fcntl


class ShellCommandExecutor:
    def __init__(self):
        # State persistence
        self.current_dir = os.getcwd()
        self.env_vars = os.environ.copy()
        self.shell_history = []

        # PTY management
        self.master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.process: Optional[subprocess.Popen] = None
        self.original_settings = None

        # Output capture
        self.output_buffer = []
        self.capture_output = True
        self.output_callback: Optional[Callable[[str], None]] = None

        # Threading for I/O handling
        self.io_thread: Optional[threading.Thread] = None
        self.stop_io = threading.Event()

        # DYNAMIC alternate screen detection
        self.used_alternate_screen = False

    def set_output_callback(self, callback: Callable[[str], None]):
        """Set callback function to receive real-time output."""
        self.output_callback = callback

    def execute_shell_command(self, command: str) -> str:
        """Execute shell command using PTY for full terminal emulation."""
        try:
            # Store command in history
            self.shell_history.append(command)

            # Reset alternate screen tracking for this command
            self.used_alternate_screen = False

            # Ensure we start from the correct directory
            self._sync_directory_state()

            # Handle special built-in commands that need state management
            if self._handle_builtin_command(command):
                # After handling cd, make sure Python is in the right place
                self._sync_directory_state()
                return self._get_builtin_output(command)

            # Execute with PTY
            result = self._execute_with_pty(command)

            # Sync directory state after execution
            self._sync_directory_state()

            return result

        except Exception as e:
            return f"❌ Error executing command: {str(e)}"

    def _get_terminal_size(self):
        """Get current terminal size."""
        if sys.stdout.isatty():
            try:
                size = struct.unpack(
                    "hh", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, "1234")
                )
                return size
            except Exception:
                pass
        return (24, 80)  # Default size

    def _set_pty_size(self, fd):
        """Set PTY size to match current terminal."""
        if fd and sys.stdout.isatty():
            try:
                rows, cols = self._get_terminal_size()
                size = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
            except Exception:
                pass

    def _execute_with_pty(self, command: str) -> str:
        """Execute command using PTY with full terminal emulation."""
        try:
            # Clear output buffer
            self.output_buffer.clear()
            self.stop_io.clear()

            # Save terminal settings
            if sys.stdin.isatty():
                self.original_settings = termios.tcgetattr(sys.stdin.fileno())

            # Create PTY pair
            self.master_fd, self.slave_fd = pty.openpty()

            # KEEP: Set PTY size to match terminal
            self._set_pty_size(self.master_fd)

            # Get user's shell
            user_shell = os.environ.get("SHELL", "/bin/bash")

            # KEEP: Build command that explicitly sources config files and then runs the command
            if "bash" in user_shell:
                wrapped_command = f"[ -f ~/.bashrc ] && source ~/.bashrc; {command}"
            elif "zsh" in user_shell:
                wrapped_command = f"[ -f ~/.zshrc ] && source ~/.zshrc; {command}"
            else:
                wrapped_command = command

            # KEEP: Create the process with PTY (this works for aliases!)
            self.process = subprocess.Popen(
                [user_shell, "-i", "-c", wrapped_command],
                stdin=self.slave_fd,
                stdout=self.slave_fd,
                stderr=self.slave_fd,
                cwd=self.current_dir,
                env=self.env_vars,
                preexec_fn=os.setsid if os.name != "nt" else None,
            )

            # Close slave end in parent process
            os.close(self.slave_fd)
            self.slave_fd = None

            # KEEP: Set terminal to raw mode if we're in a TTY
            if sys.stdin.isatty():
                tty.setraw(sys.stdin.fileno())

            # Start I/O handling thread
            self.io_thread = threading.Thread(target=self._handle_pty_io, daemon=True)
            self.io_thread.start()

            # Wait for process completion
            exit_code = self.process.wait()

            # Stop I/O thread and give it time to capture final output
            self.stop_io.set()
            if self.io_thread:
                self.io_thread.join(timeout=1.0)

            # Update directory state
            self._update_directory_state()

            # Get the captured output for conversation history
            captured_output = "".join(self.output_buffer).strip()

            # Clean ANSI escape codes from output for better conversation history
            import re

            clean_output = re.sub(r"\x1b\[[0-9;]*[mK]", "", captured_output)
            clean_output = clean_output.strip()

            # Return appropriate output based on exit code
            if exit_code == 0:
                # For successful commands, return the clean output
                # If no output, return empty string (main.py will handle this)
                return clean_output
            else:
                # For failed commands, include error information
                error_msg = f"❌ Command exited with code {exit_code}"
                if clean_output:
                    return f"{clean_output}\n{error_msg}"
                else:
                    return error_msg

        except KeyboardInterrupt:
            self._cleanup_pty()
            return "🛑 Command interrupted"
        except Exception as e:
            self._cleanup_pty()
            return f"❌ Error executing command: {str(e)}"
        finally:
            self._cleanup_pty()

    def _handle_pty_io(self):
        """Handle bidirectional I/O between terminal and process."""
        raw_mode_set = False
        try:
            while (
                not self.stop_io.is_set()
                and self.process
                and self.process.poll() is None
            ):
                # Check for available input/output with timeout
                ready_fds = []
                try:
                    fds_to_check = [self.master_fd]
                    if sys.stdin.isatty():
                        fds_to_check.append(sys.stdin)

                    ready_fds, _, _ = select.select(fds_to_check, [], [], 0.1)
                except (OSError, ValueError):
                    break

                for fd in ready_fds:
                    try:
                        if fd == sys.stdin and sys.stdin.isatty():
                            # Read from terminal and send to process
                            data = os.read(sys.stdin.fileno(), 1024)
                            if data and self.master_fd:
                                os.write(self.master_fd, data)

                        elif fd == self.master_fd:
                            # Read from process and send to terminal/capture
                            data = os.read(self.master_fd, 4096)
                            if data:
                                # DYNAMIC: Detect alternate screen usage
                                data_str = data.decode("utf-8", errors="ignore")

                                # Check for alternate screen entry sequences
                                if any(
                                    seq in data_str
                                    for seq in [
                                        "\x1b[?1049h",  # Alternate screen buffer
                                        "\x1b[?47h",  # Alternate screen (older)
                                        "\x1b[?1047h",  # Alternate screen (xterm)
                                    ]
                                ):
                                    self.used_alternate_screen = True

                                # Check if we need to switch to raw mode
                                if not raw_mode_set and sys.stdin.isatty():
                                    # Look for signs that a program wants raw mode
                                    if any(
                                        seq in data_str
                                        for seq in [
                                            "\x1b[?1049h",  # Alternate screen buffer
                                            "\x1b[?1h",  # Application cursor keys
                                            "\x1b[?47h",  # Alternate screen
                                            "\x1b=",  # Application keypad mode
                                        ]
                                    ):
                                        try:
                                            tty.setraw(sys.stdin.fileno())
                                            raw_mode_set = True
                                        except Exception:
                                            pass

                                # Always write to terminal (shows colors!)
                                if sys.stdout.isatty():
                                    os.write(sys.stdout.fileno(), data)
                                    sys.stdout.flush()

                                # Capture output for non-interactive commands
                                try:
                                    text = data.decode("utf-8", errors="replace")
                                    self.output_buffer.append(text)

                                    # Call output callback if set
                                    if self.output_callback:
                                        self.output_callback(text)
                                except Exception:
                                    pass
                            else:
                                # EOF from process
                                break

                    except (OSError, ValueError):
                        # Handle broken pipe or closed file descriptor
                        break

        except Exception:
            # Log error but don't crash
            pass
        finally:
            # Restore terminal if we set raw mode
            if raw_mode_set and self.original_settings and sys.stdin.isatty():
                try:
                    termios.tcsetattr(
                        sys.stdin.fileno(), termios.TCSADRAIN, self.original_settings
                    )
                except Exception:
                    pass

    def _cleanup_pty(self):
        """Clean up PTY resources and restore terminal settings."""
        try:
            # Stop I/O thread
            self.stop_io.set()

            # DYNAMIC CURSOR FIX: Only fix cursor if alternate screen was used
            if self.used_alternate_screen and sys.stdout.isatty():
                try:
                    # Send minimal terminal reset sequences
                    reset_sequences = [
                        "\x1b[?1049l",  # Exit alternate screen buffer
                        "\x1b[?47l",  # Exit alternate screen (older)
                        "\x1b[?1047l",  # Exit alternate screen (xterm)
                        "\x1b[0m",  # Reset all attributes
                    ]

                    for seq in reset_sequences:
                        sys.stdout.write(seq)
                    sys.stdout.flush()

                    # Very small delay to let terminal process
                    time.sleep(0.005)

                except Exception:
                    pass

            # Ensure we're on a new line after the command
            if sys.stdout.isatty():
                try:
                    # Check if we need a newline
                    # This helps with commands that don't end with a newline
                    if self.output_buffer and not self.output_buffer[-1].endswith("\n"):
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                except Exception:
                    pass

            # Restore terminal settings
            if self.original_settings and sys.stdin.isatty():
                try:
                    termios.tcsetattr(
                        sys.stdin.fileno(), termios.TCSADRAIN, self.original_settings
                    )
                except Exception:
                    pass

            # Close file descriptors
            if self.master_fd:
                try:
                    os.close(self.master_fd)
                
                except Exception:
                    pass
                self.master_fd = None

            if self.slave_fd:
                try:
                    os.close(self.slave_fd)
                except Exception:
                    pass
                self.slave_fd = None

            # Clean up process
            if self.process:
                try:
                    if self.process.poll() is None:
                        # Try graceful termination first
                        os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                        time.sleep(0.1)
                        if self.process.poll() is None:
                            # Force kill if still running
                            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except Exception:
                    pass
                self.process = None

        except Exception:
            pass

    def _update_directory_state(self):
        """Try to detect if the working directory changed during command execution."""
        # After command execution, ensure Python process is in the right directory
        self._sync_directory_state()

    def _handle_builtin_command(self, command: str) -> bool:
        """Handle commands that need special state management."""
        cmd_parts = command.strip().split()
        if not cmd_parts:
            return False

        # Only handle cd for state persistence
        # Let the shell handle export, unset, etc. normally
        return cmd_parts[0] == "cd"

    def _get_builtin_output(self, command: str) -> str:
        """Execute built-in commands with state persistence."""
        cmd_parts = command.strip().split()
        cmd = cmd_parts[0]

        try:
            if cmd == "cd":
                return self._handle_cd_command(cmd_parts)
            else:
                return ""

        except Exception as e:
            return f"❌ Error in builtin command: {str(e)}"

    def _handle_cd_command(self, cmd_parts: list) -> str:
        """Handle cd command with proper state update."""
        if len(cmd_parts) == 1:
            new_dir = os.path.expanduser("~")
        else:
            target = cmd_parts[1]

            if target == "-":
                if hasattr(self, "previous_dir"):
                    new_dir = self.previous_dir
                else:
                    return "❌ No previous directory"
            elif target.startswith("~"):
                new_dir = os.path.expanduser(target)
            elif os.path.isabs(target):
                new_dir = target
            else:
                new_dir = os.path.join(self.current_dir, target)

        try:
            resolved_path = os.path.abspath(new_dir)
            if os.path.isdir(resolved_path):
                self.previous_dir = self.current_dir
                self.current_dir = resolved_path

                # ACTUALLY change the Python process's working directory
                os.chdir(resolved_path)

                # Update the environment variable
                self.env_vars["PWD"] = resolved_path
                return ""
            else:
                return f"❌ cd: no such file or directory: {cmd_parts[1] if len(cmd_parts) > 1 else '~'}"
        except Exception as e:
            return f"❌ cd: {str(e)}"

    def interrupt_current_command(self):
        """Interrupt currently running command."""
        try:
            if self.process and self.process.poll() is None:
                # Send SIGINT to process group
                os.killpg(os.getpgid(self.process.pid), signal.SIGINT)
                return True
        except Exception:
            pass
        return False

    def get_current_directory(self) -> str:
        """Get the current working directory."""
        return self.current_dir

    def get_shell_history(self) -> list:
        """Get the shell command history."""
        return self.shell_history.copy()

    def get_last_output(self) -> str:
        """Get the captured output from the last command."""
        return "".join(self.output_buffer)

    def reset_state(self):
        """Reset shell state to initial values."""
        self.current_dir = os.getcwd()
        self.env_vars = os.environ.copy()
        self.shell_history = []
        self.output_buffer = []

    def _sync_directory_state(self):
        """Synchronize Python's working directory with tracked state."""
        try:
            current_actual = os.getcwd()
            if current_actual != self.current_dir:
                # Update both Python's working directory and our tracking
                os.chdir(self.current_dir)
        except Exception:
            # If we can't change to tracked directory, update tracking to match reality
            self.current_dir = os.getcwd()
            self.env_vars["PWD"] = self.current_dir
