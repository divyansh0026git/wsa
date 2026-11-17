# Public release checklist

The current Git remote points to an upstream project. Create a new repository under
`divyansh0026git` and explicitly set its remote before publishing; do not push to
the existing remote by accident. Preserve the existing MIT license and attribution.

Include the Python packages, browser extension, tests, setup scripts, example
configuration, README and license. Exclude local accounts, cookies, relay sessions,
personal configurations, virtual environments, test artifacts and logs. Ignore
rules do not remove files already tracked: review the complete staged file list and
the full Git history for secrets before publishing. Rotate any exposed credentials.
Review the browser extension's broad host and cookie permissions before release.

Run the Windows suite and macOS installation, CLI, shell, background-process and
browser-bridge checks on real machines. Browser websites and their availability can
change, so report the providers and platforms actually tested.
