package com.click.shell;

import java.net.URI;
import java.net.URISyntaxException;

final class ReaderApiUrlPolicy {
    private ReaderApiUrlPolicy() {
    }

    static String resolve(String baseUrl, String pathOrUrl) {
        try {
            URI base = checkedBase(baseUrl);
            String candidateValue = pathOrUrl == null ? "" : pathOrUrl.trim();
            if (candidateValue.isEmpty()) {
                throw new IllegalArgumentException("Reader API URL is empty");
            }
            URI candidate = new URI(candidateValue);
            URI resolved = candidate.isAbsolute()
                    ? candidate
                    : base.resolve(candidateValue.startsWith("/") ? candidateValue : "/" + candidateValue);
            checkedHttpUri(resolved, "Reader API URL");
            if (!sameOrigin(base, resolved)) {
                throw new SecurityException("Reader API URL is not same-origin");
            }
            return resolved.normalize().toASCIIString();
        } catch (URISyntaxException error) {
            throw new IllegalArgumentException("Reader API URL is invalid", error);
        }
    }

    static boolean sameOrigin(String baseUrl, String candidateUrl) {
        try {
            return sameOrigin(checkedBase(baseUrl), new URI(candidateUrl));
        } catch (URISyntaxException | IllegalArgumentException error) {
            return false;
        }
    }

    private static URI checkedBase(String value) throws URISyntaxException {
        URI base = new URI(value == null ? "" : value.trim());
        checkedHttpUri(base, "Reader API base URL");
        if (base.getRawQuery() != null || base.getRawFragment() != null) {
            throw new IllegalArgumentException("Reader API base URL must not contain query or fragment");
        }
        String path = base.getRawPath();
        if (path != null && !path.isEmpty() && !"/".equals(path)) {
            throw new IllegalArgumentException("Reader API base URL must be an origin");
        }
        return new URI(
                base.getScheme().toLowerCase(java.util.Locale.ROOT),
                null,
                base.getHost().toLowerCase(java.util.Locale.ROOT),
                base.getPort(),
                "/",
                null,
                null
        );
    }

    private static void checkedHttpUri(URI uri, String label) {
        String scheme = uri.getScheme();
        if (
                !uri.isAbsolute()
                        || scheme == null
                        || (!"http".equalsIgnoreCase(scheme) && !"https".equalsIgnoreCase(scheme))
                        || uri.getHost() == null
                        || uri.getHost().trim().isEmpty()
                        || uri.getRawUserInfo() != null
                        || uri.getRawFragment() != null
        ) {
            throw new IllegalArgumentException(label + " must be an absolute HTTP(S) URL without credentials or fragment");
        }
    }

    private static boolean sameOrigin(URI left, URI right) {
        checkedHttpUri(left, "Reader API base URL");
        checkedHttpUri(right, "Reader API URL");
        return left.getScheme().equalsIgnoreCase(right.getScheme())
                && left.getHost().equalsIgnoreCase(right.getHost())
                && effectivePort(left) == effectivePort(right);
    }

    private static int effectivePort(URI uri) {
        if (uri.getPort() >= 0) {
            return uri.getPort();
        }
        return "https".equalsIgnoreCase(uri.getScheme()) ? 443 : 80;
    }
}
