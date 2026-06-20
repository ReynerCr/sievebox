# Sandbox-run

Works by having a configured set of core and specific tooling permissions, combines them and then run the specific tools in a newly created sandbox with some shiny messages and indicators. Some of its already created profiles are for Conda, Node (npm, pnpm, yarn, bun, etc; combines every specific tool folders in a profile for the sandbox), Llama.cpp, Opencode and Pi Agent.

To run an app (e.g. bash) inside the sandbox and that is registered in it (having a profile configured) use:

```bash
$ sandbox-run bash
```

It will print some info about the current real path and the executed app and will execute the app:

```bash
======================================================
 Entering Sandboxed Container Engine
 Host Path:  /path/to/your/current/shell/sesion
 Executing:  bash
======================================================

# some warnings related to the --new-sesion argument in Bubblewrap

# app execution, in this case a new bash shell where you can run any command allowed in the sandbox
[sandbox] /path/to/your/current/shell/sesion$ 
```

## Overriding binaries

For easy of use and security (so you can't easily run the executables outside a sandbox), you can setup overrides to wrap the apps in the sandbox on the bashrc (or similar files) by overriding the already existing binaries located in one of the folders in the $PATH.

In the dev folder I have all my extensions for my ~/.bashrc. In concrete, the "10-override.sh" file contains the overrides that I did setup for my existing tooling and it can be used as basis for configuring other overrides.

With this done, now you can just run the app and the overrides will execute it wrapped in sandbox-run. For confirmation, check that the sandboxing info is printed on the shell.

Note: this is not a catch-all solution. If a command is wrapped inside others, the sandbox may not run with this catch (e.g. running the command with strace OR using the complete path of the binary instead of the abbreviation are some ways to ignore the overrides).

## Extending the profiles and apps

*This is WIP so take it with a grain of salt*

It is not that easy to extend the sandbox but the parts that are easily editable and its instructions are commented with "**" prefixed. Look for "# **" at the start of the lines.

The file sandbox-profiles.sh contains the different user profiles. Those can be extended using the same structure (it is explained in the comments of the script). Also, to extend apps, you need to add more PROFILE_DEPS that creates the profiles per se with its submodules. Optionally, to change the prompt color (if the module for an app is not the first in the list of PROFILE_DEPS), set PROFILE_ROOT_MOD to the profile with the color that you want.

Be mindful that if you need D-BUS or X11 then they both pose a security risk if not managed properly. In case of D-BUS, you can use a proxy that manages access permissions, which is a common practice. Configured sandbox profiles don't enable D-BUS and also unsets the XAUTHORITY variable that helps control the X11 problems.

## Testing file permissions

To test for file access errors (like permission errors), you can use sandbox-run like this:

```bash
$ sandbox-run --discover app
```

This will run strace internally with the engine and will output in ~/.local/state/sandbox-run/discovery the logs from the run: the raw output of strace, multiple procesed files from the raw and a summary made with some heuristics and rules from the engine that should be useful for determining missing file permissions.

Also you can use the --list flag and it will show you all the registered profiles with its submodules (the engine supports inheritance).

```bash
$ sandbox-run list
```
