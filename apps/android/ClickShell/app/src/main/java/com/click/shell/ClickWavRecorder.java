package com.click.shell;

import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.SystemClock;

import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;

/** One process-local PCM WAV recorder shared by native Tingle and bridged voice capture. */
final class ClickWavRecorder implements AutoCloseable {
    private static final int SAMPLE_RATE = 16_000;
    private static final int CHANNELS = 1;
    private static final int BITS_PER_SAMPLE = 16;
    static final long MAX_AUDIO_DATA_BYTES = 24L * 1024L * 1024L;

    private AudioRecord audioRecord;
    private Thread writerThread;
    private volatile boolean recording;
    private volatile Throwable writerFailure;
    private File audioFile;
    private long startedAtElapsed;
    private final Runnable sizeLimitReached;

    ClickWavRecorder() {
        this(null);
    }

    ClickWavRecorder(Runnable sizeLimitReached) {
        this.sizeLimitReached = sizeLimitReached;
    }

    synchronized void start(File destination) throws IOException {
        if (recording || audioRecord != null) {
            throw new IOException("WAV recorder is already running");
        }
        if (destination == null || destination.getParentFile() == null
                || !destination.getParentFile().isDirectory()) {
            throw new IOException("WAV destination directory is unavailable");
        }
        int channelConfig = AudioFormat.CHANNEL_IN_MONO;
        int audioFormat = AudioFormat.ENCODING_PCM_16BIT;
        int minimum = AudioRecord.getMinBufferSize(SAMPLE_RATE, channelConfig, audioFormat);
        if (minimum <= 0) {
            throw new IOException("Cannot create WAV recorder buffer");
        }
        int bufferSize = Math.max(minimum, SAMPLE_RATE);
        AudioRecord next = new AudioRecord(
                MediaRecorder.AudioSource.MIC,
                SAMPLE_RATE,
                channelConfig,
                audioFormat,
                bufferSize
        );
        if (next.getState() != AudioRecord.STATE_INITIALIZED) {
            next.release();
            throw new IOException("WAV recorder is not initialized");
        }

        audioFile = destination;
        audioRecord = next;
        writerFailure = null;
        recording = true;
        startedAtElapsed = SystemClock.elapsedRealtime();
        Thread nextWriter = new Thread(
                () -> writeRecording(next, destination, bufferSize),
                "click-native-wav-recorder"
        );
        writerThread = nextWriter;
        try {
            next.startRecording();
            nextWriter.start();
        } catch (Throwable error) {
            recording = false;
            writerThread = null;
            audioRecord = null;
            try {
                next.release();
            } catch (Throwable ignored) {
                // Best-effort initialization cleanup.
            }
            throw new IOException("Cannot start WAV recorder", error);
        }
    }

    synchronized boolean isRecording() {
        return recording;
    }

    synchronized Result stopAndFinalize() throws IOException {
        AudioRecord current = audioRecord;
        Thread writer = writerThread;
        File destination = audioFile;
        long startedAt = startedAtElapsed;
        recording = false;
        audioRecord = null;
        writerThread = null;
        audioFile = null;
        startedAtElapsed = 0L;
        if (current == null || destination == null) {
            throw new IOException("WAV recorder is not running");
        }
        try {
            if (current.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                current.stop();
            }
        } catch (Throwable ignored) {
            // Releasing below also unblocks a pending read.
        }
        if (writer != null) {
            try {
                writer.join(2_000L);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                throw new IOException("Interrupted while finalizing WAV recording", interrupted);
            }
        }
        try {
            current.release();
        } catch (Throwable ignored) {
            // Best-effort platform cleanup.
        }
        if (writer != null && writer.isAlive()) {
            throw new IOException("WAV writer did not stop");
        }
        Throwable failure = writerFailure;
        writerFailure = null;
        if (failure != null) {
            throw new IOException("Cannot write WAV recording", failure);
        }
        if (!destination.isFile() || destination.length() <= 44L) {
            throw new IOException("WAV recording has no audio samples");
        }
        double durationSeconds = Math.max(
                0.1,
                (SystemClock.elapsedRealtime() - startedAt) / 1_000.0
        );
        return new Result(destination, durationSeconds);
    }

    @Override
    public synchronized void close() {
        if (audioRecord == null) {
            return;
        }
        try {
            stopAndFinalize();
        } catch (Throwable ignored) {
            // The private WAV remains available for repository recovery on the next app start.
        }
    }

    private void writeRecording(AudioRecord source, File destination, int bufferSize) {
        long dataLength = 0L;
        boolean reachedSizeLimit = false;
        byte[] buffer = new byte[Math.max(2_048, bufferSize)];
        try (RandomAccessFile output = new RandomAccessFile(destination, "rw")) {
            output.setLength(0L);
            writeWavHeader(output, 0L);
            while (recording) {
                int read = source.read(buffer, 0, buffer.length);
                if (read > 0) {
                    int allowed = (int) Math.min(
                            read,
                            Math.max(0L, MAX_AUDIO_DATA_BYTES - dataLength)
                    );
                    if (allowed > 0) {
                        output.write(buffer, 0, allowed);
                        dataLength += allowed;
                    }
                    if (dataLength >= MAX_AUDIO_DATA_BYTES) {
                        reachedSizeLimit = true;
                        recording = false;
                    }
                }
            }
            updateWavHeader(output, dataLength);
            output.getFD().sync();
        } catch (Throwable error) {
            writerFailure = error;
            recording = false;
        }
        if (reachedSizeLimit && sizeLimitReached != null) {
            try {
                sizeLimitReached.run();
            } catch (Throwable ignored) {
                // The WAV is already finalized and remains recoverable if the UI is gone.
            }
        }
    }

    static void repairHeader(File file) throws IOException {
        if (file == null || !file.isFile() || file.length() <= 44L) {
            throw new IOException("Interrupted WAV has no audio samples");
        }
        try (RandomAccessFile output = new RandomAccessFile(file, "rw")) {
            updateWavHeader(output, file.length() - 44L);
            output.getFD().sync();
        }
    }

    private static void writeWavHeader(RandomAccessFile output, long dataLength) throws IOException {
        long byteRate = (long) SAMPLE_RATE * CHANNELS * BITS_PER_SAMPLE / 8L;
        int blockAlign = CHANNELS * BITS_PER_SAMPLE / 8;
        output.writeBytes("RIFF");
        writeLittleEndianInt(output, 36L + dataLength);
        output.writeBytes("WAVE");
        output.writeBytes("fmt ");
        writeLittleEndianInt(output, 16L);
        writeLittleEndianShort(output, 1);
        writeLittleEndianShort(output, CHANNELS);
        writeLittleEndianInt(output, SAMPLE_RATE);
        writeLittleEndianInt(output, byteRate);
        writeLittleEndianShort(output, blockAlign);
        writeLittleEndianShort(output, BITS_PER_SAMPLE);
        output.writeBytes("data");
        writeLittleEndianInt(output, dataLength);
    }

    private static void updateWavHeader(RandomAccessFile output, long dataLength) throws IOException {
        output.seek(4L);
        writeLittleEndianInt(output, 36L + dataLength);
        output.seek(40L);
        writeLittleEndianInt(output, dataLength);
    }

    private static void writeLittleEndianInt(RandomAccessFile output, long value) throws IOException {
        output.write((int) (value & 0xff));
        output.write((int) ((value >> 8) & 0xff));
        output.write((int) ((value >> 16) & 0xff));
        output.write((int) ((value >> 24) & 0xff));
    }

    private static void writeLittleEndianShort(RandomAccessFile output, int value) throws IOException {
        output.write(value & 0xff);
        output.write((value >> 8) & 0xff);
    }

    static final class Result {
        final File audioFile;
        final double durationSeconds;

        Result(File audioFile, double durationSeconds) {
            this.audioFile = audioFile;
            this.durationSeconds = durationSeconds;
        }
    }
}
