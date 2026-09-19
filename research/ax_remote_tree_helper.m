// Read-only macOS host for tvOS 26's remoteAXService.
//
// The helper connects to a localhost byte bridge and hands that socket to
// Apple's macOS AccessibilityAudit host implementation. It only reads the
// translated tree; no accessibility actions or remote-control inputs exist in
// this binary.
//
// Build:
//   xcrun clang -fobjc-arc -fblocks -framework Foundation -framework AppKit \
//     research/ax_remote_tree_helper.m -o /tmp/ax-remote-tree-helper
//
// Framework-only check (no network/device access):
//   /tmp/ax-remote-tree-helper --self-test

#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>

#include <arpa/inet.h>
#include <dlfcn.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

@interface AXAuditRemoteDevice : NSObject
@property(nonatomic, readonly) id accessibilityOverlayView;
- (instancetype)initWithFileDescriptor:(int)descriptor
                             identifier:(NSString *)identifier
                             deviceSize:(CGSize)size;
- (void)startAccessibility;
- (void)stopAccessibility;
@end

@interface NSObject (AXReadOnlyProbe)
- (NSArray *)accessibilityChildren;
- (NSString *)accessibilityLabel;
- (NSString *)accessibilityRole;
- (id)accessibilityValue;
- (NSString *)accessibilityIdentifier;
- (id)accessibilityAttributeValue:(NSString *)attribute;
@end

static BOOL LoadFramework(NSString *path) {
    void *handle = dlopen(path.fileSystemRepresentation, RTLD_LAZY | RTLD_LOCAL);
    if (handle == NULL) {
        fprintf(stderr, "dlopen failed for %s: %s\n", path.UTF8String, dlerror());
        return NO;
    }
    return YES;
}

static int ConnectLocalhost(uint16_t port) {
    int descriptor = socket(AF_INET, SOCK_STREAM, 0);
    if (descriptor < 0) {
        perror("socket");
        return -1;
    }

    struct sockaddr_in address = {0};
    address.sin_family = AF_INET;
    address.sin_port = htons(port);
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(descriptor, (const struct sockaddr *)&address, sizeof(address)) != 0) {
        perror("connect");
        close(descriptor);
        return -1;
    }
    return descriptor;
}

static id ReadObject(id element, SEL selector) {
    if (element == nil || ![element respondsToSelector:selector]) {
        return nil;
    }
    id (*implementation)(id, SEL) = (id (*)(id, SEL))[element methodForSelector:selector];
    return implementation(element, selector);
}

static id JSONScalar(id value) {
    if (value == nil) return [NSNull null];
    if ([value isKindOfClass:NSString.class] ||
        [value isKindOfClass:NSNumber.class] ||
        [value isKindOfClass:NSNull.class]) {
        return value;
    }
    return [value description] ?: [NSNull null];
}

static NSDictionary *ReadNode(id element, NSUInteger depth,
                              NSUInteger maxDepth, NSUInteger *remaining) {
    if (element == nil || *remaining == 0) return @{};
    *remaining -= 1;

    NSMutableDictionary *node = [NSMutableDictionary dictionary];
    id role = ReadObject(element, @selector(accessibilityRole));
    id label = ReadObject(element, @selector(accessibilityLabel));
    id value = ReadObject(element, @selector(accessibilityValue));
    id identifier = ReadObject(element, @selector(accessibilityIdentifier));
    node[@"role"] = JSONScalar(role);
    node[@"label"] = JSONScalar(label);
    node[@"value"] = JSONScalar(value);
    node[@"identifier"] = JSONScalar(identifier);

    if ([element respondsToSelector:@selector(accessibilityAttributeValue:)]) {
        id focused = [element accessibilityAttributeValue:NSAccessibilityFocusedAttribute];
        if (focused != nil) node[@"focused"] = JSONScalar(focused);
    }
    if ([element respondsToSelector:@selector(accessibilityFrame)]) {
        CGRect (*readFrame)(id, SEL) = (CGRect (*)(id, SEL))[
            element methodForSelector:@selector(accessibilityFrame)
        ];
        node[@"frame"] = NSStringFromRect(readFrame(element, @selector(accessibilityFrame)));
    }

    if (depth < maxDepth && *remaining > 0) {
        id childrenValue = ReadObject(element, @selector(accessibilityChildren));
        if ([childrenValue isKindOfClass:NSArray.class]) {
            NSMutableArray *children = [NSMutableArray array];
            for (id child in (NSArray *)childrenValue) {
                if (*remaining == 0) break;
                [children addObject:ReadNode(child, depth + 1, maxDepth, remaining)];
            }
            if (children.count > 0) node[@"children"] = children;
        }
    }
    return node;
}

static BOOL PrintTreeIfReady(AXAuditRemoteDevice *device) {
    id overlay = device.accessibilityOverlayView;
    id root = ReadObject(
        overlay, NSSelectorFromString(@"_accessibilityTranslationAppElement")
    );
    if (root == nil) {
        id children = ReadObject(overlay, @selector(accessibilityChildren));
        if (![children isKindOfClass:NSArray.class] || [(NSArray *)children count] == 0) {
            return NO;
        }
        root = overlay;
    }

    NSUInteger remaining = 500;
    NSDictionary *tree = ReadNode(root, 0, 8, &remaining);
    NSError *error = nil;
    NSData *json = [NSJSONSerialization dataWithJSONObject:tree
                                                   options:NSJSONWritingPrettyPrinted
                                                     error:&error];
    if (json == nil) {
        fprintf(stderr, "JSON serialization failed: %s\n", error.description.UTF8String);
        return NO;
    }
    fwrite(json.bytes, 1, json.length, stdout);
    fputc('\n', stdout);
    fflush(stdout);
    return YES;
}

static void PrintConnectionState(AXAuditRemoteDevice *device, NSUInteger second) {
    id version = nil;
    id manager = nil;
    id overlay = nil;
    @try {
        version = [device valueForKey:@"deviceAPIVersion"];
        manager = [device valueForKey:@"hostCacheManager"];
        overlay = [device valueForKey:@"accessibilityOverlayView"];
    } @catch (NSException *exception) {
        fprintf(stderr, "state KVC failed: %s\n", exception.name.UTF8String);
    }
    fprintf(stderr, "state t=%lus api=%s manager=%s overlay=%s\n",
            (unsigned long)second,
            [[version description] ?: @"nil" UTF8String],
            manager == nil ? "nil" : object_getClassName(manager),
            overlay == nil ? "nil" : object_getClassName(overlay));
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSArray<NSString *> *arguments = NSProcessInfo.processInfo.arguments;
        NSString *platformPath = @"/System/Library/PrivateFrameworks/"
                                 "AccessibilityPlatformTranslation.framework/"
                                 "AccessibilityPlatformTranslation";
        NSString *auditPath = @"/System/Library/PrivateFrameworks/"
                              "AccessibilityAudit.framework/AccessibilityAudit";
        if (!LoadFramework(platformPath) || !LoadFramework(auditPath)) return 2;

        Class deviceClass = NSClassFromString(@"AXAuditRemoteDevice");
        Class elementClass = NSClassFromString(@"AXPMacPlatformElement");
        if (deviceClass == Nil || elementClass == Nil) {
            fprintf(stderr, "required private classes unavailable\n");
            return 3;
        }
        if ([arguments containsObject:@"--self-test"]) {
            printf("frameworks=ok remote_device=ok platform_element=ok\n");
            return 0;
        }

        NSUInteger portIndex = [arguments indexOfObject:@"--port"];
        if (portIndex == NSNotFound || portIndex + 1 >= arguments.count) {
            fprintf(stderr, "usage: ax-remote-tree-helper --port PORT [--timeout SECONDS]\n");
            return 64;
        }
        NSInteger portValue = arguments[portIndex + 1].integerValue;
        if (portValue < 1 || portValue > UINT16_MAX) {
            fprintf(stderr, "invalid port\n");
            return 64;
        }
        NSTimeInterval timeout = 15.0;
        NSUInteger timeoutIndex = [arguments indexOfObject:@"--timeout"];
        if (timeoutIndex != NSNotFound && timeoutIndex + 1 < arguments.count) {
            timeout = MAX(1.0, arguments[timeoutIndex + 1].doubleValue);
        }

        int descriptor = ConnectLocalhost((uint16_t)portValue);
        if (descriptor < 0) return 4;
        AXAuditRemoteDevice *device = [[deviceClass alloc]
            initWithFileDescriptor:descriptor
                        identifier:@"apple-tv-observation-probe"
                        deviceSize:CGSizeMake(1920.0, 1080.0)];
        if (device == nil) {
            fprintf(stderr, "AXAuditRemoteDevice initialization failed\n");
            close(descriptor);
            return 5;
        }

        [device startAccessibility];
        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:timeout];
        BOOL printed = NO;
        NSUInteger ticks = 0;
        while (!printed && deadline.timeIntervalSinceNow > 0) {
            @autoreleasepool {
                [[NSRunLoop currentRunLoop] runUntilDate:
                    [NSDate dateWithTimeIntervalSinceNow:0.1]];
                printed = PrintTreeIfReady(device);
                ticks += 1;
                if (!printed && ticks % 10 == 0) {
                    PrintConnectionState(device, ticks / 10);
                }
            }
        }
        [device stopAccessibility];
        if (!printed) {
            fprintf(stderr, "remote accessibility tree did not become ready\n");
            return 6;
        }
        return 0;
    }
}
