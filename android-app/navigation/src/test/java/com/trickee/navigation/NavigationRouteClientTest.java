package com.trickee.navigation;

import org.junit.Test;

import static org.junit.Assert.*;

public class NavigationRouteClientTest {
    private static final NavigationRoute.Point START = new NavigationRoute.Point(12.9716, 77.5946);
    private static final NavigationRoute.Point END = new NavigationRoute.Point(12.7409, 77.8253);

    @Test public void buildsBoundedOsrmRouteRequest() {
        NavigationRouteClient client = new NavigationRouteClient("https://router.example/", false);
        String path = client.requestPath(START, END);
        assertTrue(path.startsWith("route/v1/driving/77.5946000,12.9716000;77.8253000,12.7409000"));
        assertTrue(path.contains("geometries=geojson"));
        assertTrue(path.contains("steps=true"));
    }

    @Test public void rejectsUnsafeProviderUrls() {
        rejects("", false);
        rejects("http://router.example/", false);
        rejects("http://router.example/", true);
        rejects("https://user:secret@router.example/", false);
        rejects("https://router.example/?token=secret", false);
        rejects("file:///tmp/router", true);
        new NavigationRouteClient("http://127.0.0.1:8010/", true);
        rejects("http://127.0.0.1:8010/", false);
    }

    @Test public void rejectsInvalidRequestCoordinates() {
        NavigationRouteClient client = new NavigationRouteClient("https://router.example/", false);
        try {
            client.requestPath(new NavigationRoute.Point(100, 77), END);
            fail("latitude should be rejected");
        } catch (IllegalArgumentException expected) {
            // Do not send invalid location data to a provider.
        }
    }

    private static void rejects(String url, boolean debug) {
        try {
            new NavigationRouteClient(url, debug);
            fail("expected unsafe provider rejection: " + url);
        } catch (IllegalArgumentException expected) {
            // Provider configuration is intentionally constrained.
        }
    }
}
