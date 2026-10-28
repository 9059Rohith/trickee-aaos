(() => {
  const status = document.getElementById("accountAccessStatus");
  const links = document.getElementById("accountAccessLinks");
  const retry = document.getElementById("accountAccessRetry");
  async function load() {
    retry.disabled = true;
    status.textContent = "Loading account access options…";
    links.replaceChildren();
    try {
      const response = await fetch("/api/v1/account-access", {
        cache: "no-store",
        signal: AbortSignal.timeout(15000),
      });
      if (!response.ok) throw new Error("Account configuration unavailable");
      const data = await response.json();
      for (const kind of ["login", "registration", "recovery", "security"])
        if (data[kind + "_url"]) {
          const link = document.createElement("a");
          link.textContent = `Open identity-provider ${kind}`;
          link.href = data[kind + "_url"];
          link.rel = "noopener noreferrer";
          link.style.cssText = "display:block;min-height:44px;padding:12px";
          links.append(link);
        }
      status.textContent = data.recovery_url
        ? "Use the configured provider below. Passwords and reset links are handled by that provider."
        : "No identity-provider recovery link is configured. Contact your administrator for account recovery. No reset email has been sent.";
    } catch {
      status.textContent =
        "Account access options could not load. Retry or contact your administrator.";
    } finally {
      retry.disabled = false;
    }
  }
  retry.addEventListener("click", load);
  void load();
})();
