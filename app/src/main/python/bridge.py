"""Glue between the Android screen and nse.py / universe_in.py. The two tool files are the pack's own, unchanged."""
import contextlib
import io
import json
import os
import shutil
import tempfile


def _prepare(workdir):
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)  # the tools keep their files in ./trader
    os.makedirs("trader", exist_ok=True)


def run(workdir, joined_args):
    """Run one command and return everything it printed. joined_args: arguments separated by newlines."""
    _prepare(workdir)
    argv = [a for a in joined_args.split("\n") if a]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            if argv and argv[0] == "universe":
                import universe_in
                universe_in.main(["build"] + argv[1:])
            else:
                import nse
                nse.main(argv)
        except SystemExit:
            pass
        except Exception as exc:  # show it on screen rather than crash the app
            print("Error: %s" % exc)
    return buf.getvalue().strip() or "(no output)"


def read_config(workdir):
    _prepare(workdir)
    path = os.path.join("trader", "config-in.json")
    if not os.path.exists(path):
        import nse
        nse.main(["init"])
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write_config(workdir, text):
    """Save the limits file. Returns '' when saved, or a plain reason when refused."""
    _prepare(workdir)
    try:
        json.loads(text)
    except ValueError as exc:
        return "Not saved: that is not valid JSON (%s)" % exc
    import nse
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    tmp.write(text)
    tmp.close()
    try:
        nse.load_config(tmp.name)
    except nse.NseError as exc:
        os.unlink(tmp.name)
        return "Not saved: %s" % exc
    shutil.move(tmp.name, os.path.join("trader", "config-in.json"))
    return ""
