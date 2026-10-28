package com.trickee.driver;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import org.json.JSONObject;
final class WorkspaceRules {
  static JSONObject preferences(String displayName, String zone, boolean reminders) {
    if (displayName == null
        || displayName.trim().codePointCount(0, displayName.trim().length()) > 80)
      throw new IllegalArgumentException("Display name must be at most 80 characters");
    try {
      ZoneId.of(zone);
      return new JSONObject()
          .put("display_name", displayName.trim())
          .put("timezone", zone)
          .put("reminders", reminders);
    } catch (java.time.DateTimeException | org.json.JSONException invalid) {
      throw new IllegalArgumentException("Select an IANA time zone", invalid);
    }
  }
  static boolean freshSafety(long verifiedAt, long now) {
    return verifiedAt > 0 && now >= verifiedAt && now - verifiedAt < 8000;
  }
  static boolean parked(JSONObject twin) {
    if (twin == null || !"OK".equals(twin.optString("quality")))
      return false;
    return "PARKED".equals(twin.optString("state")) || "CHARGING".equals(twin.optString("state"));
  }
  static String pagePath(String resource, int offset) {
    if (resource == null
        || !resource.matches("/(events|notifications|conversations/[A-Za-z0-9_-]{1,80}/messages)")
        || offset < 0 || offset > 100000)
      throw new IllegalArgumentException("Invalid workspace page");
    return "/api/v1/driver/workspace" + resource + "?offset=" + offset + "&limit=100";
  }
  static String id(String value) {
    if (value == null || !value.matches("[A-Za-z0-9_-]{1,80}"))
      throw new IllegalArgumentException("Invalid workspace identifier");
    return value;
  }
  static void dates(String start, String end, String zone) {
    try {
      ZoneId.of(zone);
      java.time.Instant first = OffsetDateTime.parse(start).toInstant(),
                        last = OffsetDateTime.parse(end).toInstant();
      if (!last.isAfter(first)
          || java.time.Duration.between(first, last).compareTo(java.time.Duration.ofDays(366)) > 0)
        throw new IllegalArgumentException("End must follow start within one year");
    } catch (java.time.DateTimeException invalid) {
      throw new IllegalArgumentException(
          "Use ISO dates with UTC offset and an IANA time zone", invalid);
    }
  }
}
