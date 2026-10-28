package com.trickee.driver;
import static org.junit.Assert.*;

import org.json.JSONObject;
import org.junit.Test;
public class WorkspaceRulesTest {
  @Test
  public void failClosedForVehicleState() throws Exception {
    for (String state : new String[] {"DRIVING", "UNKNOWN", "OFFLINE", "STOPPED", "FAULT"})
      assertFalse(WorkspaceRules.parked(new JSONObject().put("quality", "OK").put("state", state)));
    assertFalse(WorkspaceRules.parked(null));
    assertFalse(
        WorkspaceRules.parked(new JSONObject().put("quality", "STALE").put("state", "PARKED")));
    assertTrue(WorkspaceRules.parked(new JSONObject().put("quality", "OK").put("state", "PARKED")));
  }
  @Test
  public void requiresExplicitOffsetAndOrderedDates() {
    WorkspaceRules.dates("2026-10-17T09:00:00+05:30", "2026-10-17T10:00:00+05:30", "Asia/Kolkata");
    assertThrows(IllegalArgumentException.class,
        () -> WorkspaceRules.dates("2026-10-17T09:00", "2026-10-17T10:00", "UTC"));
    assertThrows(IllegalArgumentException.class,
        () -> WorkspaceRules.dates("2026-10-17T10:00Z", "2026-10-17T09:00Z", "UTC"));
  }
  @Test
  public void pagesOnlyAllowWorkspaceResourcesAndBoundedOffsets() {
    assertEquals("/api/v1/driver/workspace/events?offset=100&limit=100",
        WorkspaceRules.pagePath("/events", 100));
    for (String resource : new String[] {"/me", "/events?x=1", "/conversations/../messages"})
      assertThrows(IllegalArgumentException.class, () -> WorkspaceRules.pagePath(resource, 0));
    assertThrows(IllegalArgumentException.class, () -> WorkspaceRules.pagePath("/events", -1));
  }
  @Test
  public void rejectsMoreThanExactlyOneYear() {
    assertThrows(IllegalArgumentException.class,
        () -> WorkspaceRules.dates("2024-01-01T00:00Z", "2025-01-01T01:00Z", "UTC"));
  }
  @Test
  public void staleSafetyVerificationHidesPrivateContent() {
    assertFalse(WorkspaceRules.freshSafety(1000, 9001));
    assertFalse(WorkspaceRules.freshSafety(0, 1));
    assertTrue(WorkspaceRules.freshSafety(1000, 8999));
  }
  @Test
  public void validatesPreferenceZoneAndDisplayName() throws Exception {
    assertEquals("Asia/Kolkata",
        WorkspaceRules.preferences(" Driver ", "Asia/Kolkata", true).getString("timezone"));
    assertEquals(
        "Driver", WorkspaceRules.preferences(" Driver ", "UTC", false).getString("display_name"));
    assertThrows(IllegalArgumentException.class,
        () -> WorkspaceRules.preferences("Driver", "Mars/Invalid", true));
    assertThrows(IllegalArgumentException.class,
        () -> WorkspaceRules.preferences("x".repeat(81), "UTC", true));
  }
  @Test
  public void identifiersCannotEscapeEndpoint() {
    assertEquals("abc123", WorkspaceRules.id("abc123"));
    for (String id : new String[] {"../me", "a?x=1", "a/b", "", "a%2fb"})
      assertThrows(IllegalArgumentException.class, () -> WorkspaceRules.id(id));
  }
}
