"""Het Koersbord — portfolio and terminal backend."""

# Bumped whenever the Python adds a route or changes a payload the pages rely
# on. The frontend checks it and says so when the running server is older than
# the files on disk — because static HTML is read from disk every request while
# routes only exist in the running process, so a stale server serves new pages
# that then 404 on their own links.
BUILD = 57
__version__ = "2.0"

# When this server started: SYS's uptime (build 53).
import time as _time
STARTED = _time.time()
