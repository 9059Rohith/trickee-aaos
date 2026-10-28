package com.trickee.navigation;

import androidx.car.app.navigation.model.Maneuver;
import androidx.car.app.navigation.model.Step;

/** Conservative OSRM to Car App maneuver mapping; unsupported turns get no arrow. */
final class NavigationManeuver {
    private NavigationManeuver() { }

    static int carType(NavigationRoute.Turn turn) {
        String type = turn.type.toLowerCase(java.util.Locale.ROOT);
        String modifier = turn.modifier.toLowerCase(java.util.Locale.ROOT);
        switch (type) {
            case "depart":
                return Maneuver.TYPE_DEPART;
            case "arrive":
                if ("left".equals(modifier)) return Maneuver.TYPE_DESTINATION_LEFT;
                if ("right".equals(modifier)) return Maneuver.TYPE_DESTINATION_RIGHT;
                return Maneuver.TYPE_DESTINATION;
            case "continue":
            case "new name":
                if ("slight left".equals(modifier) || "left".equals(modifier))
                    return Maneuver.TYPE_KEEP_LEFT;
                if ("slight right".equals(modifier) || "right".equals(modifier))
                    return Maneuver.TYPE_KEEP_RIGHT;
                return "straight".equals(modifier) || modifier.isEmpty()
                        ? Maneuver.TYPE_STRAIGHT : Maneuver.TYPE_UNKNOWN;
            case "turn":
                switch (modifier) {
                    case "left": return Maneuver.TYPE_TURN_NORMAL_LEFT;
                    case "right": return Maneuver.TYPE_TURN_NORMAL_RIGHT;
                    case "slight left": return Maneuver.TYPE_TURN_SLIGHT_LEFT;
                    case "slight right": return Maneuver.TYPE_TURN_SLIGHT_RIGHT;
                    case "sharp left": return Maneuver.TYPE_TURN_SHARP_LEFT;
                    case "sharp right": return Maneuver.TYPE_TURN_SHARP_RIGHT;
                    default: return Maneuver.TYPE_UNKNOWN;
                }
            case "fork":
                if (modifier.endsWith("left")) return Maneuver.TYPE_FORK_LEFT;
                if (modifier.endsWith("right")) return Maneuver.TYPE_FORK_RIGHT;
                return Maneuver.TYPE_UNKNOWN;
            case "merge":
                if (modifier.endsWith("left")) return Maneuver.TYPE_MERGE_LEFT;
                if (modifier.endsWith("right")) return Maneuver.TYPE_MERGE_RIGHT;
                return Maneuver.TYPE_MERGE_SIDE_UNSPECIFIED;
            case "on ramp":
                if ("left".equals(modifier)) return Maneuver.TYPE_ON_RAMP_NORMAL_LEFT;
                if ("right".equals(modifier)) return Maneuver.TYPE_ON_RAMP_NORMAL_RIGHT;
                if ("slight left".equals(modifier)) return Maneuver.TYPE_ON_RAMP_SLIGHT_LEFT;
                if ("slight right".equals(modifier)) return Maneuver.TYPE_ON_RAMP_SLIGHT_RIGHT;
                return Maneuver.TYPE_UNKNOWN;
            case "off ramp":
                if (modifier.endsWith("left")) return Maneuver.TYPE_OFF_RAMP_NORMAL_LEFT;
                if (modifier.endsWith("right")) return Maneuver.TYPE_OFF_RAMP_NORMAL_RIGHT;
                return Maneuver.TYPE_UNKNOWN;
            default:
                return Maneuver.TYPE_UNKNOWN;
        }
    }

    static Step step(NavigationRoute.Turn turn) {
        int type = carType(turn);
        if (type == Maneuver.TYPE_UNKNOWN) return null;
        Step.Builder builder = new Step.Builder(turn.instruction)
                .setManeuver(new Maneuver.Builder(type).build());
        if (!turn.road.isEmpty()) builder.setRoad(turn.road);
        return builder.build();
    }
}
