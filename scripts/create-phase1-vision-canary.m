#import <AppKit/AppKit.h>

// Offscreen synthetic test data only. Never reads a screen or user document.
int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 2) return 2;
        NSString *folder = [NSString stringWithUTF8String:argv[1]];
        for (int index = 1; index <= 8; index++) {
            @autoreleasepool {
                NSBitmapImageRep *bitmap = [[NSBitmapImageRep alloc]
                    initWithBitmapDataPlanes:NULL pixelsWide:1525 pixelsHigh:1318
                    bitsPerSample:8 samplesPerPixel:4 hasAlpha:YES isPlanar:NO
                    colorSpaceName:NSDeviceRGBColorSpace bytesPerRow:0 bitsPerPixel:0];
                NSGraphicsContext *context = [NSGraphicsContext graphicsContextWithBitmapImageRep:bitmap];
                [NSGraphicsContext saveGraphicsState];
                [NSGraphicsContext setCurrentContext:context];
                [[NSColor whiteColor] setFill]; NSRectFill(NSMakeRect(0, 0, 1525, 1318));
                NSDictionary *font = @{NSFontAttributeName:[NSFont monospacedSystemFontOfSize:18 weight:NSFontWeightRegular],
                    NSForegroundColorAttributeName:[NSColor blackColor]};
                [@"SYNTHETIC DISPLAY - NO PERSONAL DATA" drawAtPoint:NSMakePoint(40,1250) withAttributes:font];
                for (int row = 0; row < 32; row++) {
                    NSString *line = [NSString stringWithFormat:@"Fixture row %02d | orchard %d | valley %d | test material only", row, 200+row*3, 71+row];
                    [line drawAtPoint:NSMakePoint(40,1190-row*30) withAttributes:font];
                }
                NSString *code = [NSString stringWithFormat:@"Validation code: CEDAR-%04d", 4000+index*137];
                [code drawAtPoint:NSMakePoint(1000,40) withAttributes:font];
                [NSGraphicsContext restoreGraphicsState];
                NSData *png = [bitmap representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
                NSString *path = [folder stringByAppendingPathComponent:[NSString stringWithFormat:@"fixture-%d.png",index]];
                if (![png writeToFile:path options:NSDataWritingWithoutOverwriting error:NULL]) return 3;
            }
        }
    }
    return 0;
}
