// Read-only CoreMediaIO probe for wireless Apple TV screen-capture sources.
//
// Usage:
//   xcrun swift research/airplay_capture_probe.swift [wait-seconds]
//
// The two CMIO opt-in flags are process-local. This probe only enumerates
// capture devices: it never opens a device, starts an AVCaptureSession, or
// initiates an AirPlay connection.

import AVFoundation
import CoreMediaIO
import Darwin
import Foundation

private let systemObject = CMIOObjectID(kCMIOObjectSystemObject)

private func propertyAddress(_ selector: Int) -> CMIOObjectPropertyAddress {
    CMIOObjectPropertyAddress(
        mSelector: UInt32(selector),
        mScope: UInt32(kCMIOObjectPropertyScopeGlobal),
        mElement: UInt32(kCMIOObjectPropertyElementMain)
    )
}

private func enable(_ selector: Int, label: String) {
    var address = propertyAddress(selector)
    var enabled: UInt32 = 1
    let status = CMIOObjectSetPropertyData(
        systemObject,
        &address,
        0,
        nil,
        UInt32(MemoryLayout<UInt32>.size),
        &enabled
    )
    print("enable \(label): OSStatus=\(status)")
}

private func cmioDeviceIDs() -> [CMIOObjectID] {
    var address = propertyAddress(kCMIOHardwarePropertyDevices)
    var byteCount: UInt32 = 0
    guard CMIOObjectGetPropertyDataSize(
        systemObject,
        &address,
        0,
        nil,
        &byteCount
    ) == noErr else {
        return []
    }
    let count = Int(byteCount) / MemoryLayout<CMIOObjectID>.size
    var ids = [CMIOObjectID](repeating: 0, count: count)
    guard CMIOObjectGetPropertyData(
        systemObject,
        &address,
        0,
        nil,
        byteCount,
        &byteCount,
        &ids
    ) == noErr else {
        return []
    }
    return ids
}

private func stringProperty(_ objectID: CMIOObjectID, selector: Int) -> String? {
    var address = propertyAddress(selector)
    var byteCount = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    var value: Unmanaged<CFString>?
    let status = CMIOObjectGetPropertyData(
        objectID,
        &address,
        0,
        nil,
        byteCount,
        &byteCount,
        &value
    )
    guard status == noErr, let value else { return nil }
    return value.takeUnretainedValue() as String
}

private func uint32Property(_ objectID: CMIOObjectID, selector: Int) -> UInt32? {
    var address = propertyAddress(selector)
    var byteCount = UInt32(MemoryLayout<UInt32>.size)
    var value: UInt32 = 0
    let status = CMIOObjectGetPropertyData(
        objectID,
        &address,
        0,
        nil,
        byteCount,
        &byteCount,
        &value
    )
    return status == noErr ? value : nil
}

private func fourCC(_ value: UInt32?) -> String {
    guard let value else { return "?" }
    let bytes = [
        UInt8((value >> 24) & 0xff),
        UInt8((value >> 16) & 0xff),
        UInt8((value >> 8) & 0xff),
        UInt8(value & 0xff),
    ]
    return String(bytes: bytes, encoding: .ascii) ?? String(format: "0x%08x", value)
}

private func printInventory(reason: String) {
    let avDevices = AVCaptureDevice.devices(for: .video)
    print("[\(reason)] AVCapture video devices: \(avDevices.count)")
    for device in avDevices {
        print(
            "  AV name=\(device.localizedName) uid=\(device.uniqueID) "
                + "type=\(device.deviceType.rawValue) suspended=\(device.isSuspended)"
        )
    }

    let ids = cmioDeviceIDs()
    print("[\(reason)] CMIO devices: \(ids.count)")
    for id in ids {
        let name = stringProperty(id, selector: kCMIOObjectPropertyName) ?? "?"
        let uid = stringProperty(id, selector: kCMIODevicePropertyDeviceUID) ?? "?"
        let model = stringProperty(id, selector: kCMIODevicePropertyModelUID) ?? "?"
        let transport = fourCC(uint32Property(id, selector: kCMIODevicePropertyTransportType))
        print("  CMIO id=\(id) name=\(name) uid=\(uid) model=\(model) transport=\(transport)")
    }
    fflush(stdout)
}

let waitSeconds = max(0, Int(CommandLine.arguments.dropFirst().first ?? "20") ?? 20)
let observer = NotificationCenter.default.addObserver(
    forName: AVCaptureDevice.wasConnectedNotification,
    object: nil,
    queue: nil
) { notification in
    guard let device = notification.object as? AVCaptureDevice else { return }
    print(
        "connected name=\(device.localizedName) uid=\(device.uniqueID) "
            + "type=\(device.deviceType.rawValue)"
    )
    fflush(stdout)
}

enable(kCMIOHardwarePropertyAllowScreenCaptureDevices, label: "screen capture devices")
enable(
    kCMIOHardwarePropertyAllowWirelessScreenCaptureDevices,
    label: "wireless screen capture devices"
)
printInventory(reason: "initial")

for second in 1...max(1, waitSeconds) {
    if waitSeconds == 0 { break }
    RunLoop.current.run(until: Date(timeIntervalSinceNow: 1))
    if second % 5 == 0 || second == waitSeconds {
        printInventory(reason: "after \(second)s")
    }
}

NotificationCenter.default.removeObserver(observer)
