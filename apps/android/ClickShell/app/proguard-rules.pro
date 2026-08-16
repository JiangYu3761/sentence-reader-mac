# JNI names are resolved when the optional sherpa-onnx runtime is packaged.
-keep class com.k2fsa.sherpa.onnx.** { *; }

# Readium publication models are serialized across navigator/streamer boundaries.
-keepattributes Signature,InnerClasses,EnclosingMethod
-keepattributes RuntimeVisibleAnnotations,RuntimeInvisibleAnnotations,AnnotationDefault

# Keep native method names while allowing the rest of Click and its dependencies to shrink.
-keepclasseswithmembernames,includedescriptorclasses class * {
    native <methods>;
}

# PDFBox probes this optional commercial JPEG2000 decoder only when present.
-dontwarn com.gemalto.jp2.JP2Decoder
