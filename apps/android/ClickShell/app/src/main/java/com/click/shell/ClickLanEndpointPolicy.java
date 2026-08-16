package com.click.shell;

import java.net.Inet4Address;
import java.net.InetAddress;

/** Pure validation for Bonjour-resolved Reader API endpoints. */
final class ClickLanEndpointPolicy {
    private ClickLanEndpointPolicy() {
    }

    static String originFor(String addressValue, int port) {
        String address = addressValue == null ? "" : addressValue.trim();
        if (address.isEmpty() || port <= 0 || port > 65_535) {
            return "";
        }
        try {
            InetAddress resolved = InetAddress.getByName(address);
            if (!(resolved instanceof Inet4Address)
                    || !resolved.isSiteLocalAddress()
                    || resolved.isAnyLocalAddress()
                    || resolved.isLoopbackAddress()
                    || resolved.isMulticastAddress()) {
                return "";
            }
            return "http://" + resolved.getHostAddress() + ":" + port;
        } catch (Throwable ignored) {
            return "";
        }
    }
}
