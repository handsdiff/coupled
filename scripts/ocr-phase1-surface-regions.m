#import <Foundation/Foundation.h>
#import <Vision/Vision.h>
#import <ImageIO/ImageIO.h>

static NSDictionary *rectDictionary(CGRect rect) {
    return @{
        @"x": @(rect.origin.x),
        @"y": @(rect.origin.y),
        @"width": @(rect.size.width),
        @"height": @(rect.size.height),
    };
}

static void emit(NSDictionary *value) {
    NSError *error = nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:NSJSONWritingSortedKeys error:&error];
    if (data == nil) {
        fprintf(stderr, "could not encode result: %s\n", error.localizedDescription.UTF8String);
        exit(7);
    }
    fwrite(data.bytes, 1, data.length, stdout);
    fputc('\n', stdout);
    fflush(stdout);
}

static NSDictionary *processJob(NSDictionary *job) {
    NSString *jobID = job[@"jobID"];
    NSString *imagePath = job[@"imagePath"];
    NSDictionary *regionValue = job[@"regionOfInterest"];
    if (![jobID isKindOfClass:NSString.class]
        || ![imagePath isKindOfClass:NSString.class]
        || ![regionValue isKindOfClass:NSDictionary.class]) {
        return @{@"jobID": jobID ?: [NSNull null], @"error": @"invalid_job"};
    }

    CGRect region = CGRectMake(
        [regionValue[@"x"] doubleValue],
        [regionValue[@"y"] doubleValue],
        [regionValue[@"width"] doubleValue],
        [regionValue[@"height"] doubleValue]
    );
    CGImageSourceRef source = CGImageSourceCreateWithURL(
        (__bridge CFURLRef)[NSURL fileURLWithPath:imagePath],
        NULL
    );
    if (source == NULL) return @{@"jobID": jobID, @"error": @"image_source_unavailable"};
    CGImageRef image = CGImageSourceCreateImageAtIndex(source, 0, NULL);
    CFRelease(source);
    if (image == NULL) return @{@"jobID": jobID, @"error": @"image_unavailable"};

    VNRecognizeTextRequest *request = [[VNRecognizeTextRequest alloc] init];
    request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
    request.usesLanguageCorrection = YES;
    request.automaticallyDetectsLanguage = YES;
    request.regionOfInterest = region;
    VNImageRequestHandler *handler = [[VNImageRequestHandler alloc] initWithCGImage:image options:@{}];
    NSError *requestError = nil;
    BOOL succeeded = [handler performRequests:@[request] error:&requestError];
    CGImageRelease(image);
    if (!succeeded) {
        return @{
            @"jobID": jobID,
            @"error": requestError.localizedDescription ?: @"vision_request_failed",
        };
    }

    // Preserve Vision's observation sequence. The former fixed 0.02 row
    // tolerance could sort different lines horizontally and transpose words.
    NSArray<VNRecognizedTextObservation *> *observations = request.results;
    NSMutableArray *lines = [NSMutableArray array];
    NSMutableArray<NSString *> *strings = [NSMutableArray array];
    for (VNRecognizedTextObservation *observation in observations) {
        VNRecognizedText *candidate = [[observation topCandidates:1] firstObject];
        if (candidate == nil || candidate.string.length == 0) continue;
        [strings addObject:candidate.string];
        [lines addObject:@{
            @"text": candidate.string,
            @"confidence": @(candidate.confidence),
            @"boundingBox": rectDictionary(observation.boundingBox),
        }];
    }
    return @{
        @"jobID": jobID,
        @"orderingVersion": @"vision-native-order-v1",
        @"regionOfInterest": regionValue,
        @"content": [strings componentsJoinedByString:@"\n"],
        @"recognizedLineCount": @(lines.count),
        @"lines": lines,
    };
}

int main(void) {
    @autoreleasepool {
        char *buffer = NULL;
        size_t capacity = 0;
        while (getline(&buffer, &capacity, stdin) != -1) {
            @autoreleasepool {
                NSData *data = [[NSData alloc] initWithBytes:buffer length:strlen(buffer)];
                NSError *error = nil;
                id value = [NSJSONSerialization JSONObjectWithData:data options:0 error:&error];
                if (![value isKindOfClass:NSDictionary.class]) {
                    emit(@{@"jobID": [NSNull null], @"error": error.localizedDescription ?: @"invalid_json"});
                    continue;
                }
                emit(processJob((NSDictionary *)value));
            }
        }
        free(buffer);
    }
    return 0;
}
