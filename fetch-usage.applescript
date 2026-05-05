-- Asks Chrome to fetch claude.ai usage JSON via an already-open, logged-in claude.ai tab.
-- Requires: Chrome → View → Developer → "Allow JavaScript from Apple Events" enabled.
-- Outputs JSON string to stdout, or {"error": "..."} on failure.

on run
	set kickoff to "
        (function() {
            window.__cuOut = null;
            window.__cuStarted = Date.now();
            (async () => {
                try {
                    const orgs = await fetch('/api/organizations', {credentials: 'include'}).then(r => r.json());
                    const orgId = (Array.isArray(orgs) ? orgs[0] : orgs)?.uuid;
                    if (!orgId) throw new Error('no org id');
                    const usage = await fetch('/api/organizations/' + orgId + '/usage', {credentials: 'include'}).then(r => r.json());
                    window.__cuOut = JSON.stringify({ok: true, usage: usage, fetched_at: Date.now()});
                } catch (e) {
                    window.__cuOut = JSON.stringify({ok: false, error: String(e && e.message || e)});
                }
            })();
            'started';
        })();
    "

	set targetTab to missing value
	tell application "Google Chrome Beta"
		if not (exists window 1) then return "{\"ok\":false,\"error\":\"chrome has no windows\"}"
		repeat with w in windows
			repeat with t in tabs of w
				if URL of t starts with "https://claude.ai" then
					set targetTab to t
					exit repeat
				end if
			end repeat
			if targetTab is not missing value then exit repeat
		end repeat

		if targetTab is missing value then return "{\"ok\":false,\"error\":\"no claude.ai tab open\"}"

		tell targetTab to execute javascript kickoff
	end tell

	-- Poll for the response up to 8s
	set resp to ""
	repeat 16 times
		delay 0.5
		tell application "Google Chrome Beta"
			tell targetTab
				set resp to execute javascript "window.__cuOut || ''"
			end tell
		end tell
		if resp is not "" and resp is not missing value then exit repeat
	end repeat

	if resp is "" or resp is missing value then
		return "{\"ok\":false,\"error\":\"timeout waiting for fetch\"}"
	end if
	return resp
end run
