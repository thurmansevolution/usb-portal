/* Applied only to usbip/usbipd children through LD_PRELOAD.
 * Keep kernel-owned USB/IP sockets responsive to silent VPN/network failures.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>

static void configure(int fd)
{
    int type = 0;
    socklen_t size = sizeof(type);
    if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &size) || type != SOCK_STREAM)
        return;
    int one = 1, idle = 5, interval = 3, probes = 3, timeout = 15000;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &interval, sizeof(interval));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT, &probes, sizeof(probes));
    setsockopt(fd, IPPROTO_TCP, TCP_USER_TIMEOUT, &timeout, sizeof(timeout));
}

int socket(int domain, int type, int protocol)
{
    int (*original)(int, int, int) = dlsym(RTLD_NEXT, "socket");
    int fd = original(domain, type, protocol);
    if (fd >= 0 && (domain == AF_INET || domain == AF_INET6))
        configure(fd);
    return fd;
}

int accept(int fd, struct sockaddr *address, socklen_t *length)
{
    int (*original)(int, struct sockaddr *, socklen_t *) = dlsym(RTLD_NEXT, "accept");
    int accepted = original(fd, address, length);
    if (accepted >= 0)
        configure(accepted);
    return accepted;
}

int accept4(int fd, struct sockaddr *address, socklen_t *length, int flags)
{
    int (*original)(int, struct sockaddr *, socklen_t *, int) = dlsym(RTLD_NEXT, "accept4");
    int accepted = original(fd, address, length, flags);
    if (accepted >= 0)
        configure(accepted);
    return accepted;
}
