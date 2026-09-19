import AppIntents

/// The first system-facing action is intentionally one verb with one natural-language payload.
/// Room and title extraction belong to the authenticated hub, where configured aliases can be
/// validated immediately before execution.
@available(iOS 16.0, *)
struct ControlHomeMediaIntent: AppIntent {
    static let title: LocalizedStringResource = "Control Home Media"
    static let description = IntentDescription(
        "Turn on a configured TV, prepare a title, or set a room's volume."
    )
    static let openAppWhenRun = false

    @Parameter(
        title: "Request",
        description: "For example: Turn on Living Room and resume Archer on Hulu.",
        requestValueDialog: "What should I do, and in which room?"
    )
    var request: String

    func perform() async throws -> some IntentResult & ProvidesDialog {
        do {
            let response = try await HomeMediaClient.shared.execute(utterance: request)
            return .result(dialog: "\(response.spokenResponse)")
        } catch let error as HomeMediaClient.ClientError {
            return .result(dialog: "\(error.spokenResponse)")
        } catch {
            return .result(dialog: "The Home Media hub is unavailable.")
        }
    }
}

@available(iOS 16.0, *)
struct HomeMediaAppShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: ControlHomeMediaIntent(),
            phrases: [
                "Ask \(.applicationName) to \(\.$request)",
                "Control my TV with \(.applicationName)",
                "Tell \(.applicationName) what to play",
            ],
            shortTitle: "Control TV",
            systemImageName: "appletvremote.gen4"
        )
    }
}
