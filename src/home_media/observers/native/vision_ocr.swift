// home-media Vision OCR helper (stdlib + system frameworks only).
// Usage: vision_ocr <png-path>
// Prints one JSON object to stdout: { "tokens": [ {"text": "...", "x":0, "y":0, "w":0, "h":0 }, ... ] }
// Coordinates are normalized to image size in [0,1], origin top-left.

import AppKit
import Foundation
import Vision

struct OcrToken: Codable {
    let text: String
    let x: Double
    let y: Double
    let w: Double
    let h: Double
}

struct OcrPayload: Codable {
    let tokens: [OcrToken]
    let width: Int
    let height: Int
}

func fail(_ message: String, code: Int32 = 2) -> Never {
    FileHandle.standardError.write(Data("\(message)\n".utf8))
    exit(code)
}

guard CommandLine.arguments.count == 2 else {
    fail("usage: vision_ocr <png-path>")
}

let path = CommandLine.arguments[1]
guard let image = NSImage(contentsOfFile: path) else {
    fail("unable_to_load_image")
}
guard let tiff = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff),
      let cgImage = bitmap.cgImage
else {
    fail("unable_to_decode_image")
}

let width = cgImage.width
let height = cgImage.height
var tokens: [OcrToken] = []
let request = VNRecognizeTextRequest { request, error in
    if let error {
        fail("vision_error:\(error.localizedDescription)")
    }
    guard let observations = request.results as? [VNRecognizedTextObservation] else {
        return
    }
    for observation in observations {
        guard let best = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        // Vision uses bottom-left origin; convert to top-left normalized.
        let x = Double(box.origin.x)
        let y = Double(1.0 - box.origin.y - box.size.height)
        let w = Double(box.size.width)
        let h = Double(box.size.height)
        tokens.append(OcrToken(text: best.string, x: x, y: y, w: w, h: h))
    }
}
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    fail("vision_perform_failed:\(error.localizedDescription)")
}

let payload = OcrPayload(tokens: tokens, width: width, height: height)
do {
    let data = try JSONEncoder().encode(payload)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
} catch {
    fail("json_encode_failed")
}
