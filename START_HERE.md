# Start here

1. Extract this entire ZIP into a folder on your Windows computer.
2. Double-click **START_WINDOWS.bat**. Python 3.11+ must be installed.
3. Your browser opens the tool. Leave the terminal window open.
4. Click **Fill sample URLs**, then **Start extraction**.
5. Wait for completion and click **JSON Records CSV** or **Full JSON**.

For your own authorized source, paste the first numbered URL into **Start URL**
and the last into **End URL**: for example, `https://example.com/people/1`
through `https://example.com/people/100000`. Both endpoints are included.
The rest of the URL must match.

Use **Pause** to stop after the current request. Saved runs can resume after
restarting the app. Failed URLs appear in the job status and full JSON export.
There is no 50,000-record cutoff; capacity depends on disk, memory, and source.
At the default delay, 100,000 URLs take at least about 28 hours plus request
and processing time.

This version handles public HTML/JSON. It does not log in, bypass access
controls, or execute JavaScript. See README.md for details.

## GitHub

This folder is ready for a GitHub repository. Upload the source files,
including `.github/workflows/tests.yml`, but keep `data/` and personal exports
out of the repository. No repository has been published automatically.

## macOS / Linux

Open a terminal in the extracted folder and run `python3 launcher.py`.
