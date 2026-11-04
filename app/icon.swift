// Draws the ИИ-Офис app icon (1024×1024 PNG): an isometric tower with a glass annex on a green island.
// Usage: swift icon.swift out.png
import AppKit

let size: CGFloat = 1024
let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()
let ctx = NSGraphicsContext.current!.cgContext

// background squircle with a soft sky gradient
let bg = NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: size, height: size), xRadius: 230, yRadius: 230)
bg.addClip()
NSGradient(colors: [NSColor(calibratedRed: 0.86, green: 0.91, blue: 0.85, alpha: 1),
                    NSColor(calibratedRed: 0.70, green: 0.82, blue: 0.68, alpha: 1)])!.draw(in: bg, angle: -90)

func poly(_ pts: [CGPoint], _ color: NSColor) {
    let p = NSBezierPath()
    p.move(to: pts[0]); pts.dropFirst().forEach { p.line(to: $0) }; p.close()
    color.setFill(); p.fill()
}
// isometric helpers: x to the right-down, y to the left-down, z up
let o = CGPoint(x: 512, y: 300), k: CGFloat = 0.5
func iso(_ x: CGFloat, _ y: CGFloat, _ z: CGFloat) -> CGPoint {
    CGPoint(x: o.x + (x - y) * 0.866, y: o.y + (x + y) * k * -1 + z + 180)
}
func block(_ x: CGFloat, _ y: CGFloat, _ z: CGFloat, _ w: CGFloat, _ d: CGFloat, _ h: CGFloat, top: NSColor, left: NSColor, right: NSColor) {
    poly([iso(x, y, z + h), iso(x + w, y, z + h), iso(x + w, y + d, z + h), iso(x, y + d, z + h)], top)
    poly([iso(x, y + d, z), iso(x + w, y + d, z), iso(x + w, y + d, z + h), iso(x, y + d, z + h)], left)
    poly([iso(x + w, y, z), iso(x + w, y + d, z), iso(x + w, y + d, z + h), iso(x + w, y, z + h)], right)
}
// island
block(-260, -170, -60, 520, 400, 40, top: NSColor(calibratedRed: 0.79, green: 0.88, blue: 0.70, alpha: 1),
      left: NSColor(calibratedRed: 0.62, green: 0.70, blue: 0.58, alpha: 1), right: NSColor(calibratedRed: 0.55, green: 0.63, blue: 0.52, alpha: 1))
// annex
block(70, -40, -20, 150, 130, 90, top: NSColor(white: 0.96, alpha: 1), left: NSColor(calibratedRed: 0.17, green: 0.19, blue: 0.23, alpha: 1),
      right: NSColor(calibratedRed: 0.22, green: 0.25, blue: 0.30, alpha: 1))
// tower: floors with dark glass and wooden fins
for i in 0..<5 {
    let z = CGFloat(i) * 70 - 20
    block(-170, -60, z, 210, 170, 62, top: NSColor(white: 0.97, alpha: 1), left: NSColor(calibratedRed: 0.17, green: 0.19, blue: 0.23, alpha: 1),
          right: NSColor(calibratedRed: 0.21, green: 0.24, blue: 0.29, alpha: 1))
    for f in stride(from: CGFloat(-160), to: 40, by: 26) {          // fins on the left face
        poly([iso(f, 110, z), iso(f + 7, 110, z), iso(f + 7, 110, z + 62), iso(f, 110, z + 62)], NSColor(calibratedRed: 0.77, green: 0.60, blue: 0.42, alpha: 1))
    }
    block(-172, -62, z + 62, 214, 174, 8, top: NSColor(white: 0.99, alpha: 1), left: NSColor(white: 0.85, alpha: 1), right: NSColor(white: 0.80, alpha: 1))
}
// agents on the roof: orange drop, teal cube, blue capsule
let dots: [(CGFloat, CGFloat, NSColor)] = [(-120, 0, NSColor(calibratedRed: 0.91, green: 0.51, blue: 0.35, alpha: 1)),
                                           (-50, 40, NSColor(calibratedRed: 0.17, green: 0.71, blue: 0.60, alpha: 1)),
                                           (-10, -20, NSColor(calibratedRed: 0.42, green: 0.56, blue: 0.96, alpha: 1))]
for (x, y, c) in dots {
    let p = iso(x, y, 370)
    c.setFill(); NSBezierPath(ovalIn: NSRect(x: p.x - 34, y: p.y, width: 68, height: 80)).fill()
    NSColor(white: 0.08, alpha: 1).setFill()
    NSBezierPath(ovalIn: NSRect(x: p.x - 16, y: p.y + 38, width: 9, height: 13)).fill()
    NSBezierPath(ovalIn: NSRect(x: p.x + 7, y: p.y + 38, width: 9, height: 13)).fill()
}
img.unlockFocus()
let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
_ = ctx
