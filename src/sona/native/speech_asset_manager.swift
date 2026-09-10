import Foundation
import Speech

struct SpeechAssetResponse: Encodable {
    let status: String
    let detail: String
    let locale: String
    let progress: Double?
    let error: String?
}

struct SpeechAssetManager {
    private static let outputLock = NSLock()

    static func run() async {
        let arguments = Array(CommandLine.arguments.dropFirst())
        let action = arguments.first ?? "status"
        let localeIdentifier = arguments.dropFirst().first ?? "zh-CN"

        guard action == "download" || action == "status" else {
            emit(status: "failed", detail: "不支持的 Speech 操作。", locale: localeIdentifier)
            return
        }

        guard #available(macOS 26.0, *) else {
            emit(status: "unsupported", detail: "此模型下载方案需要 macOS 26 或更高版本。", locale: localeIdentifier)
            return
        }

        await manageAsset(action: action, localeIdentifier: localeIdentifier)
    }

    @available(macOS 26.0, *)
    private static func manageAsset(action: String, localeIdentifier: String) async {
        emit(status: "checking", detail: "正在检查设备和简体中文离线识别支持情况。", locale: localeIdentifier)
        guard SpeechTranscriber.isAvailable else {
            emit(status: "unsupported", detail: "当前 Mac 不支持 Apple Speech。", locale: localeIdentifier)
            return
        }

        guard let locale = await SpeechTranscriber.supportedLocale(
            equivalentTo: Locale(identifier: localeIdentifier)
        ) else {
            emit(status: "unsupported", detail: "当前系统不支持该离线识别语言。", locale: localeIdentifier)
            return
        }

        let transcriber = SpeechTranscriber(locale: locale, preset: .transcription)
        let initialStatus = await AssetInventory.status(forModules: [transcriber])

        // Status queries never create installation requests or reserve resources.
        if action == "status" {
            emitStatus(initialStatus, locale: localeIdentifier)
            return
        }

        switch initialStatus {
        case .installed:
            emit(status: "installed", detail: "离线语言资源已安装。", locale: localeIdentifier)
        case .unsupported:
            emit(status: "unsupported", detail: "当前系统不支持该离线识别语言。", locale: localeIdentifier)
        case .downloading, .supported:
            do {
                emit(status: "preparing", detail: "正在向系统申请离线语言资源。", locale: localeIdentifier)
                if let request = try await AssetInventory.assetInstallationRequest(
                    supporting: [transcriber]
                ) {
                    // Observe Apple's progress and flush each event to the Python pipe.
                    let observation = request.progress.observe(\.fractionCompleted, options: [.initial, .new]) { progress, _ in
                        let fraction = progress.totalUnitCount > 0 ? progress.fractionCompleted : nil
                        emit(status: "downloading", detail: "系统正在下载并安装离线语言资源。",
                             locale: localeIdentifier, progress: fraction)
                    }
                    defer { observation.invalidate() }
                    try await request.downloadAndInstall()
                }

                emit(status: "verifying", detail: "正在确认离线语言资源是否可用。", locale: localeIdentifier)
                let finalStatus = await AssetInventory.status(forModules: [transcriber])
                emitStatus(finalStatus, locale: localeIdentifier)
            } catch {
                let nativeError = error as NSError
                let diagnostics = "\(nativeError.domain) (\(nativeError.code)): \(nativeError.localizedDescription)\n\(nativeError.userInfo)"
                let status = await AssetInventory.status(forModules: [transcriber])
                if status == .installed {
                    emitStatus(status, locale: localeIdentifier)
                } else if status == .downloading {
                    emit(status: "waiting", detail: "本次请求未完成，系统仍在下载或等待网络恢复。",
                         locale: localeIdentifier, error: diagnostics)
                } else {
                    emit(status: "failed", detail: "系统无法完成离线语言资源下载。",
                         locale: localeIdentifier, error: diagnostics)
                }
            }
        @unknown default:
            emit(status: "unknown", detail: "当前系统无法确定离线识别状态。", locale: localeIdentifier)
        }
    }

    @available(macOS 26.0, *)
    private static func emitStatus(_ status: AssetInventory.Status, locale: String) {
        switch status {
        case .installed:
            emit(status: "installed", detail: "离线语言资源已安装。", locale: locale)
        case .downloading:
            emit(status: "waiting", detail: "系统仍在下载或等待网络条件恢复。", locale: locale)
        case .supported:
            emit(status: "supported", detail: "离线语言资源可供下载。", locale: locale)
        case .unsupported:
            emit(status: "unsupported", detail: "当前系统不支持该离线识别语言。", locale: locale)
        @unknown default:
            emit(status: "unknown", detail: "当前系统无法确定离线识别状态。", locale: locale)
        }
    }

    private static func emit(status: String, detail: String, locale: String,
                             progress: Double? = nil, error: String? = nil) {
        let response = SpeechAssetResponse(status: status, detail: detail, locale: locale,
                                           progress: progress, error: error)
        guard let data = try? JSONEncoder().encode(response) else {
            return
        }
        outputLock.lock()
        defer { outputLock.unlock() }
        FileHandle.standardOutput.write(data + Data([0x0A]))
    }
}

await SpeechAssetManager.run()
