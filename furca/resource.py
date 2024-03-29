import os
import fcntl
import socket
import ipaddress
from ipaddress import IPv4Address, IPv6Address
from dataclasses import dataclass, field

from logging import getLogger
from socket import SocketType
from typing import (
    cast,
    Optional as O,
    Union as U,
    Any,
    Generic,
    Type,
    TypeVar,
    ClassVar,
    Protocol,
    Tuple,
    List,
    Dict,
    Iterator,
)
from typing_extensions import Self, TypeAlias


logger = getLogger(__name__)

T = TypeVar("T")
AddrT = TypeVar("AddrT")
IPAddress: TypeAlias = U[IPv4Address, IPv6Address]

RESOURCE_IDENTS: Dict[str, Type["Resource[Any]"]] = {}

class Resource(Protocol[T]):
    IDENTS: ClassVar[Tuple[str, ...]] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for ident in cls.IDENTS:
            RESOURCE_IDENTS[ident] = cls

    @classmethod
    def decode_spec(cls, ident: str, List: List[str]) -> O[Self]:
        ...

    def encode_spec(self) -> Tuple[str, List[str]]:
        ...

    def create(self, reuse: bool = False) -> T:
        ...

    def destroy(self, instance: T) -> None:
        ...

    def decode(self, value: str) -> O[T]:
        ...

    def encode(self, instance: T) -> str:
        ...

class CreatedResources(Protocol):
    def __iter__(self) -> Iterator[Resource[Any]]: ...
    def __getitem__(self, res: Resource[T]) -> T: ...

def encode_fd(fd: int) -> str:
    os.set_inheritable(fd, True)
    return str(fd)

def decode_fd(val: str) -> O[int]:
    try:
        fd = int(val)
    except ValueError:
        return None
    # Check if FD is valid
    try:
        return os.dup(fd)
    except OSError:
        return None

@dataclass(frozen=True)
class SocketResource(Generic[AddrT], Resource[SocketType]):
    family: int
    type: int
    addr: AddrT
    protocol: int = 0

    def _bind(self, s: SocketType, reuse: bool = False) -> None:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(cast(Any, self.addr))

    def create(self, reuse: bool = False) -> SocketType:
        s = socket.socket(self.family, self.type, proto=self.protocol)
        s.set_inheritable(True)
        self._bind(s, reuse=reuse)
        return s

    def destroy(self, instance: SocketType) -> None:
        try:
            instance.close()
        except:
            logger.exception("Error while closing socket %s (%s)", instance, self)

    def encode(self, instance: SocketType) -> str:
        return encode_fd(instance.fileno())

    def decode(self, value: str) -> O[SocketType]:
        fd = decode_fd(value)
        if fd is None:
            return None
        try:
            s = SocketType(fileno=fd)
        except:
            return None
        return s

IPAddr: TypeAlias = Tuple[O[IPAddress], int]

@dataclass(init=False, frozen=True)
class IPResource(SocketResource[Tuple[str, int]]):
    dualstack: bool

    def __init__(self, type: int, addr: IPAddr, protocol: int = 0, dualstack: O[bool] = None) -> None:
        host, port = addr

        if host and not isinstance(host, (IPv4Address, IPv6Address)):
            for (family, _type, protocol, _canon, addr_info) in socket.getaddrinfo(host, port, type=type, proto=protocol):
                if family == socket.AF_INET6:
                    host = IPv6Address(addr_info[0])
                    break
                if family == socket.AF_INET:
                    host = IPv4Address(addr_info[0])
                    break
            else:
                raise ValueError(f'can not resolve {host}')

        if isinstance(host, IPv6Address):
            family = socket.AF_INET6
            dualstack_val = dualstack or False
        elif isinstance(host, IPv4Address):
            family = socket.AF_INET
            dualstack_val = False
        else:
            if socket.has_dualstack_ipv6() and dualstack is not False:
                family = socket.AF_INET6
                dualstack_val = True
            else:
                family = socket.AF_INET
                dualstack_val = False

        if dualstack_val and not socket.has_dualstack_ipv6():
            raise ValueError('dual-stack requested but not available')

        # needed due @dataclass(frozen=True)
        object.__setattr__(self, 'dualstack', dualstack_val)
        super().__init__(family, type, (str(host or ''), port), protocol)

    @classmethod
    def decode_addr(cls, args: List[str]) -> Tuple[O[IPAddr], O[bool], List[str]]:
        try:
            host = ipaddress.ip_address(args[0]) if args[0] else None
            port = int(args[1])
        except (KeyError, ValueError):
            return (None, args)
        if len(args) > 2 and args[2] in ('single', 'dual'):
            dualstack = args[2] == 'dual'
            ri = 3
        else:
            dualstack = 0
            ri = 2
        return ((host, port), args[ri:])

    def encode_addr(self) -> List[str]:
        host, port = self.addr
        extra = []
        if self.dualstack is not None:
            extra.append('dual' if self.dualstack else 'single')
        return [str(host) if host else "", str(port)] + extra

    @classmethod
    def check_ipv4(cls, addr: IPAddr) -> IPAddr:
        host, port = addr
        if not host:
            return IPv4Address("0.0.0.0"), port
        if isinstance(host, IPv6Address):
            raise ValueError("IPv6 address given for IPv4 socket")
        return addr

    @classmethod
    def check_ipv6(cls, addr: IPAddr) -> IPAddr:
        host, port = addr
        if not host:
            return IPv6Address("::"), port
        if isinstance(host, IPv4Address):
            raise ValueError("IPv4 address given for IPv6 socket")
        return addr

    def _bind(self, s: Tuple[str, int], reuse: bool = False) -> None:
        if self.dualstack is not None and self.family == socket.AF_INET6 and hasattr(socket, 'IPV6_V6ONLY'):
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0 if self.dualstack else 1)
        if reuse:
            if hasattr(socket, "SO_REUSEPORT"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            else:
                raise ValueError("can not reuse socket (no SO_REUSEPORT available)")
        return super()._bind(s, reuse=reuse)

@dataclass(init=False, frozen=True)
class TCPResource(IPResource):
    IDENTS = ("tcp", "tcp4", "tcp6")

    def __init__(self, addr: IPAddr, dualstack: O[bool] = None) -> None:
        super().__init__(socket.SOCK_STREAM, addr, protocol=socket.IPPROTO_TCP, dualstack=dualstack)

    @classmethod
    def decode_spec(cls, ident: str, args: List[str]) -> O[Self]:
        addr, dualstack, args = cls.decode_addr(args)
        if not addr:
            return None
        if ident == "tcp4":
            addr = cls.check_ipv4(addr)
        elif ident == "tcp6":
            addr = cls.check_ipv6(addr)
        return cls(addr, dualstack=dualstack)

    def encode_spec(self) -> Tuple[str, List[str]]:
        host, port = self.addr
        if not host:
            ident = "tcp"
        elif isinstance(host, IPv4Address):
            ident = "tcp4"
        elif isinstance(host, IPv6Address):
            ident = "tcp6"
        return ident, self.encode_addr()

@dataclass(init=False, frozen=True)
class UDPResource(IPResource):
    IDENTS = ("udp", "udp4", "udp6")

    def __init__(self, addr: IPAddr, dualstack: O[bool] = None) -> None:
        super().__init__(socket.SOCK_DGRAM, addr, protocol=socket.IPPROTO_UDP, dualstack=dualstack)

    @classmethod
    def decode_spec(cls, ident: str, args: List[str]) -> O[Self]:
        addr, dualstack, args = cls.decode_addr(args)
        if not addr:
            return None
        if ident == "udp4":
            addr = cls.check_ipv4(addr)
        elif ident == "udp6":
            addr = cls.check_ipv6(addr)
        return cls(addr, dualstack=dualstack)

    def encode_spec(self) -> Tuple[str, List[str]]:
        host, port = self.addr
        if not host:
            ident = "udp"
        if isinstance(host, IPv4Address):
            ident = "udp4"
        elif isinstance(host, IPv6Address):
            ident = "udp6"
        return ident, self.encode_addr()

def encode_resource_spec(resource: Resource[Any]) -> str:
    ident, args = resource.encode_spec()
    return f'{ident},{",".join(args)}'

def decode_resource_spec(value: str) -> O[Resource[Any]]:
    ident, *args = value.split(",")
    if ident not in RESOURCE_IDENTS:
        return None
    try:
        return RESOURCE_IDENTS[ident].decode_spec(ident, list(args))
    except:
        return None

def encode_resource_values(resources: CreatedResources) -> str:
    r = []
    for rtype in resources:
        spec = encode_resource_spec(rtype)
        rvalue = resources[rtype]
        value = rtype.encode(rvalue)
        r.append(f"{value},{spec}")
    return ";".join(r)

def decode_resource_values(values: str) -> CreatedResources:
    r = {}
    for v in values.split(";"):
        value, spec = v.split(",", 1)
        rtype = decode_resource_spec(spec)
        if not rtype:
            raise ValueError(f'invalid resource specification: {spec}')
        if rtype not in r:
            rvalue = rtype.decode(value)
            if rvalue is None:
                r[rtype] = rtype.create()
            else:
                r[rtype] = rvalue
    return r
