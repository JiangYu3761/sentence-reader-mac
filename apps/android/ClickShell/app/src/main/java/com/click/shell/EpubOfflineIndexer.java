package com.click.shell;

import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStream;
import java.net.URLDecoder;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

import javax.xml.parsers.DocumentBuilder;
import javax.xml.parsers.DocumentBuilderFactory;
import javax.xml.parsers.ParserConfigurationException;

import org.xml.sax.SAXException;

/**
 * Builds the durable, offline-searchable chapter rows for a locally imported EPUB.
 */
final class EpubOfflineIndexer {
    private static final int MAX_PACKAGE_BYTES = 2 * 1024 * 1024;
    private static final long MAX_INDEXED_BYTES = 120L * 1024L * 1024L;
    private static final Pattern XML_ENCODING = Pattern.compile(
            "(?is)^\\s*<\\?xml[^>]*encoding\\s*=\\s*['\"]([^'\"]+)['\"]"
    );
    private static final Pattern HTML_CHARSET = Pattern.compile(
            "(?is)<meta[^>]+charset\\s*=\\s*['\"]?([^\\s'\"/>;]+)"
    );

    private EpubOfflineIndexer() {
    }

    static List<Chapter> index(File epub) throws Exception {
        if (epub == null || !epub.isFile() || epub.length() <= 0L) {
            throw new IllegalArgumentException("EPUB source is missing");
        }
        try (ZipFile archive = new ZipFile(epub)) {
            String container = readUtf8(
                    archive,
                    requireEntry(archive, "META-INF/container.xml"),
                    MAX_PACKAGE_BYTES
            );
            Document containerXml = parseXml(container.getBytes(StandardCharsets.UTF_8));
            Element rootfile = firstElement(containerXml, "rootfile");
            String packagePath = safeEntryPath(rootfile.getAttribute("full-path"));
            if (packagePath.isEmpty()) {
                throw new IllegalArgumentException("EPUB container has no package path");
            }

            byte[] packageBytes = readBytes(
                    archive,
                    requireEntry(archive, packagePath),
                    MAX_PACKAGE_BYTES
            );
            Document packageXml = parseXml(packageBytes);
            String packageDirectory = parentPath(packagePath);
            Map<String, String> manifest = new HashMap<>();
            NodeList items = elements(packageXml, "item");
            for (int index = 0; index < items.getLength(); index++) {
                Element item = (Element) items.item(index);
                String id = clean(item.getAttribute("id"));
                String href = clean(item.getAttribute("href"));
                String mediaType = clean(item.getAttribute("media-type")).toLowerCase(Locale.ROOT);
                if (!id.isEmpty()
                        && !href.isEmpty()
                        && ("application/xhtml+xml".equals(mediaType)
                        || "text/html".equals(mediaType))) {
                    manifest.put(id, resolveEntryPath(packageDirectory, href));
                }
            }

            List<Chapter> chapters = new ArrayList<>();
            long indexedBytes = 0L;
            NodeList itemrefs = elements(packageXml, "itemref");
            for (int index = 0; index < itemrefs.getLength(); index++) {
                Element itemref = (Element) itemrefs.item(index);
                String entryPath = manifest.get(clean(itemref.getAttribute("idref")));
                if (entryPath == null) {
                    continue;
                }
                ZipEntry entry = requireEntry(archive, entryPath);
                long remaining = MAX_INDEXED_BYTES - indexedBytes;
                if (remaining <= 0L) {
                    throw new IllegalArgumentException("EPUB text is too large to index safely");
                }
                byte[] bytes = readBytes(archive, entry, remaining);
                indexedBytes += bytes.length;
                String html = decodeMarkup(bytes);
                chapters.add(new Chapter(
                        chapters.size(),
                        entryPath,
                        chapterTitle(html, entryPath),
                        html
                ));
            }
            if (chapters.isEmpty()) {
                throw new IllegalArgumentException("EPUB spine has no readable text chapters");
            }
            return chapters;
        }
    }

    private static Document parseXml(byte[] bytes) throws Exception {
        String declarationScan = new String(bytes, StandardCharsets.ISO_8859_1)
                .toUpperCase(Locale.ROOT);
        if (declarationScan.contains("<!DOCTYPE")
                || declarationScan.contains("<!ENTITY")) {
            throw new SecurityException("EPUB XML declarations are not allowed");
        }
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        factory.setNamespaceAware(true);
        factory.setExpandEntityReferences(false);
        trySetFeature(factory, "http://apache.org/xml/features/disallow-doctype-decl", true);
        trySetFeature(factory, "http://xml.org/sax/features/external-general-entities", false);
        trySetFeature(factory, "http://xml.org/sax/features/external-parameter-entities", false);
        trySetFeature(factory, "http://apache.org/xml/features/nonvalidating/load-external-dtd", false);
        DocumentBuilder builder = factory.newDocumentBuilder();
        builder.setEntityResolver((publicId, systemId) -> {
            throw new SAXException("EPUB external entities are not allowed");
        });
        return builder.parse(new ByteArrayInputStream(bytes));
    }

    private static void trySetFeature(
            DocumentBuilderFactory factory,
            String feature,
            boolean value
    ) {
        try {
            factory.setFeature(feature, value);
        } catch (ParserConfigurationException | UnsupportedOperationException | AbstractMethodError ignored) {
            // Android's platform XML provider does not implement every Xerces feature.
            // Explicit declaration rejection and the EntityResolver remain authoritative.
        }
    }

    private static NodeList elements(Document document, String localName) {
        NodeList namespaced = document.getElementsByTagNameNS("*", localName);
        return namespaced.getLength() > 0
                ? namespaced
                : document.getElementsByTagName(localName);
    }

    private static Element firstElement(Document document, String localName) {
        NodeList matches = elements(document, localName);
        if (matches.getLength() == 0 || matches.item(0).getNodeType() != Node.ELEMENT_NODE) {
            throw new IllegalArgumentException("EPUB XML is missing " + localName);
        }
        return (Element) matches.item(0);
    }

    private static byte[] readBytes(ZipFile archive, ZipEntry entry, long limit)
            throws Exception {
        if (entry.isDirectory() || limit <= 0L || (entry.getSize() > limit && entry.getSize() >= 0L)) {
            throw new IllegalArgumentException("EPUB entry is too large");
        }
        try (InputStream input = archive.getInputStream(entry);
             ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[32 * 1024];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                if (read == 0) {
                    continue;
                }
                if (output.size() > limit - read) {
                    throw new IllegalArgumentException("EPUB entry is too large");
                }
                output.write(buffer, 0, read);
            }
            return output.toByteArray();
        }
    }

    private static String readUtf8(ZipFile archive, ZipEntry entry, int limit)
            throws Exception {
        return new String(readBytes(archive, entry, limit), StandardCharsets.UTF_8);
    }

    private static ZipEntry requireEntry(ZipFile archive, String path) {
        String safe = safeEntryPath(path);
        ZipEntry entry = archive.getEntry(safe);
        if (entry == null || entry.isDirectory()) {
            throw new IllegalArgumentException("EPUB is missing " + safe);
        }
        return entry;
    }

    private static String resolveEntryPath(String base, String rawHref) throws Exception {
        String encodedPath = rawHref.split("#", 2)[0].replace("+", "%2B");
        String decoded = URLDecoder.decode(encodedPath, "UTF-8");
        return safeEntryPath(base.isEmpty() ? decoded : base + "/" + decoded);
    }

    private static String safeEntryPath(String raw) {
        String value = clean(raw).replace('\\', '/');
        while (value.startsWith("/")) {
            value = value.substring(1);
        }
        List<String> segments = new ArrayList<>();
        for (String segment : value.split("/")) {
            if (segment.isEmpty() || ".".equals(segment)) {
                continue;
            }
            if ("..".equals(segment)) {
                if (segments.isEmpty()) {
                    throw new SecurityException("EPUB path escapes archive root");
                }
                segments.remove(segments.size() - 1);
            } else {
                segments.add(segment);
            }
        }
        return String.join("/", segments);
    }

    private static String parentPath(String path) {
        int slash = path.lastIndexOf('/');
        return slash < 0 ? "" : path.substring(0, slash);
    }

    private static String decodeMarkup(byte[] bytes) {
        int sampleLength = Math.min(bytes.length, 4096);
        String sample = new String(bytes, 0, sampleLength, StandardCharsets.ISO_8859_1);
        Matcher xml = XML_ENCODING.matcher(sample);
        Matcher html = HTML_CHARSET.matcher(sample);
        String charsetName = xml.find() ? xml.group(1) : html.find() ? html.group(1) : "UTF-8";
        try {
            return new String(bytes, Charset.forName(charsetName));
        } catch (Throwable ignored) {
            return new String(bytes, StandardCharsets.UTF_8);
        }
    }

    private static String chapterTitle(String html, String entryPath) {
        for (String tag : new String[]{"h1", "h2", "title"}) {
            Matcher matcher = Pattern.compile(
                    "(?is)<" + tag + "\\b[^>]*>(.*?)</" + tag + ">"
            ).matcher(html);
            if (matcher.find()) {
                String title = matcher.group(1)
                        .replaceAll("(?is)<[^>]+>", " ")
                        .replace("&amp;", "&")
                        .replace("&lt;", "<")
                        .replace("&gt;", ">")
                        .replaceAll("\\s+", " ")
                        .trim();
                if (!title.isEmpty()) {
                    return title;
                }
            }
        }
        String name = entryPath.substring(entryPath.lastIndexOf('/') + 1);
        int dot = name.lastIndexOf('.');
        return dot > 0 ? name.substring(0, dot) : name;
    }

    private static String clean(String value) {
        return value == null ? "" : value.trim();
    }

    static final class Chapter {
        final int index;
        final String locator;
        final String title;
        final String html;

        Chapter(int index, String locator, String title, String html) {
            this.index = index;
            this.locator = locator;
            this.title = title;
            this.html = html;
        }
    }
}
