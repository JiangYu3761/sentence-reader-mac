package com.click.shell

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.MediaMetadata
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.os.Build
import android.os.IBinder

class ReaderTtsPlaybackService : Service() {
    private lateinit var mediaSession: MediaSession

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        mediaSession = MediaSession(this, "ClickReaderTts").apply {
            setCallback(
                object : MediaSession.Callback() {
                    override fun onPlay() {
                        if (ReaderTtsSession.current().state == ReaderTtsState.PAUSED) {
                            ReaderTtsSession.pauseOrResume()
                        }
                    }

                    override fun onPause() {
                        if (ReaderTtsSession.current().state == ReaderTtsState.PLAYING) {
                            ReaderTtsSession.pauseOrResume()
                        }
                    }

                    override fun onSkipToPrevious() = ReaderTtsSession.previous()

                    override fun onSkipToNext() = ReaderTtsSession.next()

                    override fun onStop() = stopPlayback()
                },
            )
            isActive = true
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_PREVIOUS -> ReaderTtsSession.previous()
            ACTION_PLAY_PAUSE -> ReaderTtsSession.pauseOrResume()
            ACTION_NEXT -> ReaderTtsSession.next()
            ACTION_STOP -> {
                stopPlayback()
                return START_NOT_STICKY
            }
        }
        val snapshot = ReaderTtsSession.current()
        updateMediaSession(snapshot)
        val notification = notification(snapshot)
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        if (::mediaSession.isInitialized) {
            mediaSession.isActive = false
            mediaSession.release()
        }
        super.onDestroy()
    }

    private fun stopPlayback() {
        ReaderTtsSession.stop()
        stopForeground(true)
        stopSelf()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Click 朗读", NotificationManager.IMPORTANCE_LOW).apply {
                description = "阅读朗读的暂停、继续和上下句控制"
                setShowBadge(false)
            },
        )
    }

    private fun notification(snapshot: ReaderTtsSnapshot): Notification {
        val paused = snapshot.state == ReaderTtsState.PAUSED
        val contentIntent = openReaderIntent()
        mediaSession.setSessionActivity(contentIntent)
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentTitle(snapshot.engineLabel)
            .setContentText(snapshot.currentText.ifBlank { snapshot.message }.take(64))
            .setSubText(if (snapshot.total > 0) "${snapshot.index + 1}/${snapshot.total} · ${snapshot.ratePercent}%" else snapshot.message)
            .setContentIntent(contentIntent)
            .setShowWhen(false)
            .setOnlyAlertOnce(true)
            .setCategory(Notification.CATEGORY_TRANSPORT)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .setOngoing(snapshot.state == ReaderTtsState.PLAYING || snapshot.state == ReaderTtsState.PREPARING)
            .addAction(android.R.drawable.ic_media_previous, "上一句", actionIntent(ACTION_PREVIOUS, 5101))
            .addAction(
                if (paused) android.R.drawable.ic_media_play else android.R.drawable.ic_media_pause,
                if (paused) "继续" else "暂停",
                actionIntent(ACTION_PLAY_PAUSE, 5102),
            )
            .addAction(android.R.drawable.ic_media_next, "下一句", actionIntent(ACTION_NEXT, 5103))
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "停止", actionIntent(ACTION_STOP, 5104))
            .setStyle(
                Notification.MediaStyle()
                    .setMediaSession(mediaSession.sessionToken)
                    .setShowActionsInCompactView(0, 1, 2),
            )
            .build()
    }

    private fun updateMediaSession(snapshot: ReaderTtsSnapshot) {
        val playbackState = when (snapshot.state) {
            ReaderTtsState.PLAYING -> PlaybackState.STATE_PLAYING
            ReaderTtsState.PAUSED -> PlaybackState.STATE_PAUSED
            ReaderTtsState.PREPARING -> PlaybackState.STATE_BUFFERING
            ReaderTtsState.ERROR -> PlaybackState.STATE_ERROR
            ReaderTtsState.IDLE, ReaderTtsState.STOPPED -> PlaybackState.STATE_STOPPED
        }
        val actions = PlaybackState.ACTION_PLAY or
            PlaybackState.ACTION_PAUSE or
            PlaybackState.ACTION_PLAY_PAUSE or
            PlaybackState.ACTION_SKIP_TO_PREVIOUS or
            PlaybackState.ACTION_SKIP_TO_NEXT or
            PlaybackState.ACTION_STOP
        val stateBuilder = PlaybackState.Builder()
            .setActions(actions)
            .setState(
                playbackState,
                snapshot.index.toLong(),
                if (snapshot.state == ReaderTtsState.PLAYING) 1f else 0f,
            )
        if (snapshot.state == ReaderTtsState.ERROR) {
            stateBuilder.setErrorMessage(snapshot.message)
        }
        mediaSession.setPlaybackState(stateBuilder.build())
        mediaSession.setMetadata(
            MediaMetadata.Builder()
                .putString(MediaMetadata.METADATA_KEY_TITLE, snapshot.currentText.ifBlank { snapshot.message }.take(160))
                .putString(MediaMetadata.METADATA_KEY_ARTIST, snapshot.engineLabel)
                .putLong(MediaMetadata.METADATA_KEY_TRACK_NUMBER, (snapshot.index + 1).toLong())
                .putLong(MediaMetadata.METADATA_KEY_NUM_TRACKS, snapshot.total.toLong())
                .build(),
        )
    }

    private fun openReaderIntent(): PendingIntent {
        val intent = ReaderTtsSession.readerIntent(this)
            ?: Intent(this, MainActivity::class.java).apply {
                addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
            }
        return PendingIntent.getActivity(
            this,
            5100,
            intent,
            pendingFlags(),
        )
    }

    private fun actionIntent(action: String, requestCode: Int): PendingIntent =
        PendingIntent.getService(
            this,
            requestCode,
            Intent(this, ReaderTtsPlaybackService::class.java).setAction(action),
            pendingFlags(),
        )

    private fun pendingFlags(): Int = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE

    companion object {
        private const val CHANNEL_ID = "click-reader-tts"
        private const val NOTIFICATION_ID = 5100
        private const val ACTION_SHOW = "com.click.shell.tts.SHOW"
        private const val ACTION_PREVIOUS = "com.click.shell.tts.PREVIOUS"
        private const val ACTION_PLAY_PAUSE = "com.click.shell.tts.PLAY_PAUSE"
        private const val ACTION_NEXT = "com.click.shell.tts.NEXT"
        private const val ACTION_STOP = "com.click.shell.tts.STOP"

        fun show(context: Context, snapshot: ReaderTtsSnapshot) {
            val intent = Intent(context, ReaderTtsPlaybackService::class.java).setAction(ACTION_SHOW)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(intent)
            else context.startService(intent)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, ReaderTtsPlaybackService::class.java))
        }
    }
}
