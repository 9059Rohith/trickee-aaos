package com.trickee.navigation;

import org.json.JSONException;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

import static org.junit.Assert.*;

public class NavigationRouteTest {
    private static final NavigationRoute.Point ORIGIN = new NavigationRoute.Point(12.9700, 77.5900);
    private static final NavigationRoute.Point DESTINATION = new NavigationRoute.Point(12.9800, 77.6000);
    private static final String GOOD = "{\"code\":\"Ok\",\"routes\":[{\"distance\":1600," +
            "\"duration\":240,\"geometry\":{\"type\":\"LineString\",\"coordinates\":[" +
            "[77.5900,12.9700],[77.5950,12.9750],[77.6000,12.9800]]}," +
            "\"legs\":[{\"steps\":[{\"name\":\"Hosur Road\",\"maneuver\":{" +
            "\"type\":\"turn\",\"modifier\":\"right\"}}]}]}]}";

    @Test public void acceptsBoundedRouteAndLabelsProvider() throws Exception {
        NavigationRoute route = NavigationRoute.parse(GOOD, ORIGIN, DESTINATION);
        assertEquals(3, route.shape.size());
        assertEquals(1600, route.distanceMetres, 0.01);
        assertTrue(route.turns.isEmpty());
        assertTrue(route.summary().contains("Provider road route"));
        assertTrue(route.summary().contains("right onto Hosur Road"));
    }

    @Test public void rejectsMissingAndFalseRoutes() {
        rejects(GOOD.replace("\"code\":\"Ok\"", "\"code\":\"NoRoute\""));
        rejects(GOOD.replace("\"distance\":1600", "\"distance\":-1"));
        rejects(GOOD.replace("\"duration\":240", "\"duration\":0"));
        rejects(GOOD.replace("[77.6000,12.9800]", "[999,12.9800]"));
        rejects(GOOD.replace("[77.6000,12.9800]", "[77.7000,13.0800]"));
        rejects(GOOD.replace("\"type\":\"LineString\"", "\"type\":\"Point\""));
        rejects("{\"code\":\"Ok\",\"routes\":[]}");
        rejects("not JSON");
    }

    @Test public void boundsProviderText() throws Exception {
        NavigationRoute route = NavigationRoute.parse(GOOD.replace("Hosur Road", "Hosur\\nRoad"),
                ORIGIN, DESTINATION);
        assertFalse(route.firstInstruction.contains("\n"));
        assertEquals("", NavigationRoute.safeText("\u0000\n", 8));
    }

    @Test public void parsesOrderedProviderTurnsOnShape() throws Exception {
        NavigationRoute route = NavigationRoute.parse(withTurns(), ORIGIN, DESTINATION);
        assertEquals(2, route.turns.size());
        assertTrue(route.turns.get(0).distanceAlongMetres > 0);
        assertTrue(route.turns.get(1).distanceAlongMetres > route.turns.get(0).distanceAlongMetres);
        assertEquals("turn right onto Hosur Road", route.turns.get(0).instruction);
    }

    @Test public void rejectsOffShapeAndOutOfOrderTurns() throws Exception {
        JSONObject offShape = new JSONObject(withTurns());
        offShape.getJSONArray("routes").getJSONObject(0).getJSONArray("legs")
                .getJSONObject(0).getJSONArray("steps").getJSONObject(0)
                .getJSONObject("maneuver").put("location", new JSONArray().put(77.7000).put(12.9750));
        rejects(offShape.toString());
        JSONObject backwards = new JSONObject(withTurns());
        JSONArray steps = backwards.getJSONArray("routes").getJSONObject(0)
                .getJSONArray("legs").getJSONObject(0).getJSONArray("steps");
        Object first = steps.get(0);
        steps.put(0, steps.get(1));
        steps.put(1, first);
        rejects(backwards.toString());
    }

    @Test public void rejectsUnsplitAntimeridianLineString() throws Exception {
        JSONObject body = new JSONObject(GOOD);
        body.getJSONArray("routes").getJSONObject(0).put("distance", 2500).put("duration", 300)
                .getJSONObject("geometry").put("coordinates", new JSONArray()
                        .put(new JSONArray().put(179.99).put(0.0))
                        .put(new JSONArray().put(-179.99).put(0.0)));
        assertThrows(JSONException.class, () -> NavigationRoute.parse(body.toString(),
                new NavigationRoute.Point(0.0, 179.99),
                new NavigationRoute.Point(0.0, -179.99)));
    }

    private static String withTurns() throws Exception {
        JSONObject body = new JSONObject(GOOD);
        JSONArray steps = body.getJSONArray("routes").getJSONObject(0)
                .getJSONArray("legs").getJSONObject(0).getJSONArray("steps");
        steps.getJSONObject(0).getJSONObject("maneuver")
                .put("location", new JSONArray().put(77.5950).put(12.9750));
        steps.put(new JSONObject().put("name", "Main Street").put("maneuver",
                new JSONObject().put("type", "arrive")
                        .put("location", new JSONArray().put(77.6000).put(12.9800))));
        return body.toString();
    }

    private static void rejects(String body) {
        try {
            NavigationRoute.parse(body, ORIGIN, DESTINATION);
            fail("expected route rejection");
        } catch (JSONException expected) {
            // Provider data is untrusted; every malformed example must fail closed.
        }
    }
}
