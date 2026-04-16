"""PyInstaller entry point — runs sayzo_agent as a package.

Handles two frozen-app quirks before click sees sys.argv:

1. Python interpreter flags (-B, -E, -I, -S, -c 'code', ...) that
   ``multiprocessing`` and its ``resource_tracker`` forward to ``sys.executable``
   when spawning subprocesses. In a PyInstaller bundle ``sys.executable`` is
   the frozen app, so those flags reach click — which rejects them with
   ``Error: No such option: -B`` and kills the resource_tracker subprocess.

2. ``multiprocessing.freeze_support()`` so the Windows spawn path works.
"""
import sys


def _handle_interpreter_flags() -> None:
    argv = sys.argv
    if len(argv) < 2 or not argv[1].startswith("-"):
        return

    # Combinable short flags emitted by
    # multiprocessing.util._args_from_interpreter_flags().
    short_flags = set("BEISsbdqvOPu")

    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "-c":
            if i + 1 >= len(argv):
                return
            code = argv[i + 1]
            sys.argv = [argv[0]] + argv[i + 2:]
            exec(code, {"__name__": "__main__", "__file__": "<string>"})
            sys.exit(0)
        if a == "-m":
            if i + 1 >= len(argv):
                return
            module = argv[i + 1]
            import runpy
            sys.argv = [argv[0]] + argv[i + 2:]
            runpy.run_module(module, run_name="__main__", alter_sys=True)
            sys.exit(0)
        if a in ("-X", "-W") and i + 1 < len(argv):
            i += 2
            continue
        if (
            len(a) >= 2
            and a[0] == "-"
            and a[1] != "-"
            and all(c in short_flags for c in a[1:])
        ):
            i += 1
            continue
        break

    if i > 1:
        sys.argv = [argv[0]] + argv[i:]


if __name__ == "__main__":
    _handle_interpreter_flags()
    import multiprocessing
    multiprocessing.freeze_support()
    from sayzo_agent.__main__ import cli
    cli()
