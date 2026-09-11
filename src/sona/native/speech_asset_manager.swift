import Foundation
import Speech
import AVFoundation
import CoreMedia

struct SpeechAssetResponse: Encodable {
    let protocol_version = 2
    let status: String
    let detail: String
    let locale: String
    let progress: Double?
    let error: String?
}

struct SpeechTranscriptionEvent: Encodable {
    let protocol_version = 2
    let kind: String
    var detail: String? = nil
    var error: String? = nil
    var start: Double? = nil
    var end: Double? = nil
    var text: String? = nil
    var language: String? = nil
    var duration: Double? = nil
}

struct SpeechAssetManager {
    private static let outputLock = NSLock()

    static func run() async {
        let arguments = Array(CommandLine.arguments.dropFirst())
        let action = arguments.first ?? "status"
        let localeIdentifier = arguments.dropFirst().first ?? "zh-CN"

        if action == "transcribe" {
            guard #available(macOS 26.0, *), arguments.count == 3 else {
                write(SpeechTranscriptionEvent(kind: "error", detail: "原生转录需要 macOS 26 或更高版本及音频文件路径。"))
                return
            }
            await transcribe(path: arguments[2], localeIdentifier: localeIdentifier)
            return
        }

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
                // Keep the language in this application's system inventory.
                try await AssetInventory.reserve(locale: locale)
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
    private static func transcribe(path: String, localeIdentifier: String) async {
        guard SpeechTranscriber.isAvailable,
              let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: localeIdentifier)) else {
            write(SpeechTranscriptionEvent(kind: "error", detail: "当前系统或设备不支持该语言的 Apple Speech 转录。"))
            return
        }
        // No volatile results: each returned range is final and appears once.
        let transcriber = SpeechTranscriber(locale: locale, preset: .transcription)
        guard await AssetInventory.status(forModules: [transcriber]) == .installed else {
            write(SpeechTranscriptionEvent(kind: "error", detail: "Apple 语言资源尚未就绪，请前往模型设置，待资源安装完成后重试。"))
            return
        }
        let analyzer = SpeechAnalyzer(modules: [transcriber])
        do {
            // Analysis never creates an installation request or downloads assets.
            try await AssetInventory.reserve(locale: locale)
            let file = try AVAudioFile(forReading: URL(fileURLWithPath: path))
            let duration = Double(file.length) / file.processingFormat.sampleRate
            guard duration.isFinite && duration > 0 else {
                throw NSError(domain: "SonaSpeech", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "音频解码后为空。"])
            }
            write(SpeechTranscriptionEvent(kind: "progress", detail: "正在加载模型。"))
            // analyzeSequence(from:) handles conversion to the module's format.
            let results = Task {
                do {
                    for try await result in transcriber.results {
                        guard result.isFinal else { continue }
                        let start = CMTimeGetSeconds(result.range.start)
                        let end = CMTimeGetSeconds(CMTimeRangeGetEnd(result.range))
                        guard start.isFinite && end.isFinite && start >= 0 && end >= start else {
                            throw NSError(domain: "SonaSpeech", code: 2,
                                          userInfo: [NSLocalizedDescriptionKey: "系统返回了无效的转录时间戳。"])
                        }
                        write(SpeechTranscriptionEvent(kind: "segment", start: start, end: end,
                                                       text: String(result.text.characters)))
                    }
                } catch {
                    await analyzer.cancelAndFinishNow()
                    throw error
                }
            }
            do {
                write(SpeechTranscriptionEvent(kind: "progress", detail: "正在转录。"))
                if let lastSample = try await analyzer.analyzeSequence(from: file) {
                    write(SpeechTranscriptionEvent(kind: "progress", detail: "正在整理转录结果。"))
                    try await analyzer.finalizeAndFinish(through: lastSample)
                } else {
                    await analyzer.cancelAndFinishNow()
                }
                // EOF alone isn't completion: wait for all final results.
                try await results.value
            } catch {
                results.cancel()
                await analyzer.cancelAndFinishNow()
                _ = try? await results.value
                throw error
            }
            write(SpeechTranscriptionEvent(kind: "result", language: locale.identifier, duration: duration))
        } catch {
            await analyzer.cancelAndFinishNow()
            let nativeError = error as NSError
            write(SpeechTranscriptionEvent(kind: "error", detail: "Apple Speech 转录失败，请检查语言资源后重试。",
                                           error: "\(nativeError.domain) (\(nativeError.code)): \(nativeError.localizedDescription)"))
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
        write(response)
    }

    private static func write<T: Encodable>(_ response: T) {
        guard let data = try? JSONEncoder().encode(response) else {
            return
        }
        outputLock.lock()
        defer { outputLock.unlock() }
        FileHandle.standardOutput.write(data + Data([0x0A]))
    }
}

await SpeechAssetManager.run()
