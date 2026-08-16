import Foundation


let tingleCommandBridgeSchema = "tingle.command_bridge.v1"


enum TingleRecordingState: String, Codable, CaseIterable {
    case idle
    case preparing
    case recording
    case paused
    case finalizing
    case savedLocally = "saved_locally"
    case failed
}


enum TingleSyncState: String, Codable {
    case ready
    case uploading
    case pending
    case synced
    case failed
}


enum TingleCommandSource: String, Codable {
    case foregroundSpace = "foreground_space"
    case globalHotKey = "global_hot_key"
    case captureButton = "capture_button"
    case menuBar = "menu_bar"
    case applicationMenu = "application_menu"
    case appIntent = "app_intent"
    case recovery
}


enum TingleCommandDisposition: String, Codable {
    case accepted
    case noOp = "no_op"
    case busy
    case unavailable
}


struct TingleRecordingSnapshot: Codable, Equatable {
    var schema = "tingle.recording_state.v1"
    var state: TingleRecordingState
    var syncState: TingleSyncState
    var captureID: String?
    var voiceRecordID: String?
    var elapsedSeconds: Double
    var inputLevel: Double = 0
    var pendingSyncCount: Int
    var errorMessage: String?

    var canStart: Bool { state == .idle }
    var canPause: Bool { state == .recording }
    var canResume: Bool { state == .paused }
    var canStopAndSave: Bool { state == .recording || state == .paused }
    var isBusy: Bool { state == .preparing || state == .finalizing }
    var originalAudioIsDurable: Bool {
        state == .savedLocally || syncState == .pending || syncState == .uploading || syncState == .synced
    }
}


enum TingleHistoryCollection: String, Codable {
    case inspirations
    case recycleBin = "recycle_bin"
}


enum TinglePlaybackState: String, Codable {
    case idle
    case playing
    case paused
}


struct TingleWebState: Codable, Equatable {
    let schema: String
    var collection: TingleHistoryCollection
    var selectedInspirationID: String?
    var selectedTitle: String?
    var selectedContentHash: String?
    var linkedExternalTargetCount: Int
    var selectedHasAudio: Bool
    var editorFocused: Bool
    var hasUnsavedChanges: Bool
    var playbackState: TinglePlaybackState
    var permanentDeleteAvailable: Bool
    var destructiveConfirmationActive: Bool?

    init(
        schema: String = tingleCommandBridgeSchema,
        collection: TingleHistoryCollection = .inspirations,
        selectedInspirationID: String? = nil,
        selectedTitle: String? = nil,
        selectedContentHash: String? = nil,
        linkedExternalTargetCount: Int = 0,
        selectedHasAudio: Bool = false,
        editorFocused: Bool = false,
        hasUnsavedChanges: Bool = false,
        playbackState: TinglePlaybackState = .idle,
        permanentDeleteAvailable: Bool = false,
        destructiveConfirmationActive: Bool? = false
    ) {
        self.schema = schema
        self.collection = collection
        self.selectedInspirationID = selectedInspirationID
        self.selectedTitle = selectedTitle
        self.selectedContentHash = selectedContentHash
        self.linkedExternalTargetCount = linkedExternalTargetCount
        self.selectedHasAudio = selectedHasAudio
        self.editorFocused = editorFocused
        self.hasUnsavedChanges = hasUnsavedChanges
        self.playbackState = playbackState
        self.permanentDeleteAvailable = permanentDeleteAvailable
        self.destructiveConfirmationActive = destructiveConfirmationActive
    }

    var isValid: Bool { schema == tingleCommandBridgeSchema }
}


enum TingleWebCommand: String, Codable, CaseIterable {
    case openInspiration = "open_inspiration"
    case focusSearch = "focus_search"
    case saveChanges = "save_changes"
    case togglePlayback = "toggle_playback"
    case archive
    case restore
    case deletePermanently = "delete_permanently"
    case showInspirations = "show_inspirations"
    case showRecycleBin = "show_recycle_bin"
    case reload
}


struct TingleCommandEnvelope: Codable, Equatable {
    let schema: String
    let commandID: String
    let command: TingleWebCommand
    let inspirationID: String?
    let deleteConfirmation: TinglePermanentDeleteConfirmation?

    init(
        command: TingleWebCommand,
        inspirationID: String?,
        deleteConfirmation: TinglePermanentDeleteConfirmation? = nil,
        commandID: String = UUID().uuidString.lowercased()
    ) {
        self.schema = tingleCommandBridgeSchema
        self.commandID = commandID
        self.command = command
        self.inspirationID = inspirationID
        self.deleteConfirmation = deleteConfirmation
    }
}


struct TinglePermanentDeleteConfirmation: Codable, Equatable {
    let confirmed: Bool
    let expectedContentHash: String?
    let acknowledgesLinkedOutputs: Bool
}


struct TingleCommandAcknowledgement: Codable, Equatable {
    let schema: String
    let commandID: String
    let ok: Bool
    let message: String?

    var isValid: Bool { schema == tingleCommandBridgeSchema && !commandID.isEmpty }
}


struct TingleCommandAvailability: Equatable {
    var canFocusSearch = true
    var canSaveChanges: Bool
    var canTogglePlayback: Bool
    var canArchive: Bool
    var canRestore: Bool
    var canDeletePermanently: Bool

    static func evaluate(webState: TingleWebState) -> TingleCommandAvailability {
        guard webState.isValid else {
            return TingleCommandAvailability(
                canFocusSearch: false,
                canSaveChanges: false,
                canTogglePlayback: false,
                canArchive: false,
                canRestore: false,
                canDeletePermanently: false
            )
        }
        let selected = webState.selectedInspirationID != nil
        return TingleCommandAvailability(
            canSaveChanges: selected && webState.hasUnsavedChanges,
            canTogglePlayback: selected && webState.selectedHasAudio,
            canArchive: selected && webState.collection == .inspirations,
            canRestore: selected && webState.collection == .recycleBin,
            canDeletePermanently: selected
                && webState.permanentDeleteAvailable
                && webState.selectedContentHash != nil
        )
    }
}


struct TingleRecordingBridgeEnvelope: Codable, Equatable {
    let schema: String
    let recordingState: TingleRecordingSnapshot

    init(recordingState: TingleRecordingSnapshot) {
        schema = tingleCommandBridgeSchema
        self.recordingState = recordingState
    }
}


struct TingleHistoryReleaseContext: Equatable {
    let scheduledGeneration: Int
    let currentGeneration: Int
    let captureSurfaceVisible: Bool
    let hasUnsavedChanges: Bool
    let destructiveConfirmationActive: Bool
    let queuedCommandCount: Int
    let inFlightCommandCount: Int

    init(
        scheduledGeneration: Int,
        currentGeneration: Int,
        captureSurfaceVisible: Bool,
        hasUnsavedChanges: Bool,
        destructiveConfirmationActive: Bool = false,
        queuedCommandCount: Int,
        inFlightCommandCount: Int
    ) {
        self.scheduledGeneration = scheduledGeneration
        self.currentGeneration = currentGeneration
        self.captureSurfaceVisible = captureSurfaceVisible
        self.hasUnsavedChanges = hasUnsavedChanges
        self.destructiveConfirmationActive = destructiveConfirmationActive
        self.queuedCommandCount = queuedCommandCount
        self.inFlightCommandCount = inFlightCommandCount
    }

    var canRelease: Bool {
        scheduledGeneration == currentGeneration
            && captureSurfaceVisible
            && !hasUnsavedChanges
            && !destructiveConfirmationActive
            && queuedCommandCount == 0
            && inFlightCommandCount == 0
    }

    var shouldRetry: Bool {
        scheduledGeneration == currentGeneration
            && captureSurfaceVisible
            && !hasUnsavedChanges
            && !destructiveConfirmationActive
            && (queuedCommandCount > 0 || inFlightCommandCount > 0)
    }
}
