// 与应用界面相同的绿色元件盒图标，无外部图片依赖。
import AppKit

let size = 1024
let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
                             bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                             isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
let context = NSGraphicsContext(bitmapImageRep: bitmap)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = context
NSColor(calibratedRed: 0.14, green: 0.30, blue: 0.23, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 56, y: 56, width: 912, height: 912), xRadius: 204, yRadius: 204).fill()
NSColor(calibratedRed: 0.93, green: 0.94, blue: 0.86, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 238, y: 243, width: 548, height: 504), xRadius: 42, yRadius: 42).fill()
NSColor(calibratedRed: 0.14, green: 0.30, blue: 0.23, alpha: 1).setFill()
for row in 0..<4 {
    for col in 0..<4 {
        NSBezierPath(roundedRect: NSRect(x: CGFloat(280 + col * 121), y: CGFloat(290 + row * 105),
                                      width: 101, height: 85), xRadius: 10, yRadius: 10).fill()
    }
}
NSColor(calibratedRed: 0.93, green: 0.94, blue: 0.86, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 208, y: 750, width: 608, height: 74), xRadius: 21, yRadius: 21).fill()
NSGraphicsContext.restoreGraphicsState()
try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
