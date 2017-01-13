# Licenced under the txaws licence available at /LICENSE in the txaws source.

"""
Client wrapper for Amazon's Route53 (domain service).
"""

from __future__ import print_function, unicode_literals

__all__ = [
    "get_route53_client",
]

from io import BytesIO
from hashlib import sha256
from operator import itemgetter

import attr
from attr import validators

from twisted.web.http import OK, CREATED
from twisted.web.client import FileBodyProducer
from twisted.internet.defer import succeed

from txaws.exception import AWSError
from txaws.client.base import RequestDetails, url_context, query, error_wrapper
from txaws.service import REGION_US_EAST_1, AWSServiceEndpoint
from txaws.util import XML

from ._util import maybe_bytes_to_unicode, to_xml, tags
from .model import HostedZone, Name, SOA, NS, CNAME

# Route53 is has two endpoints both in us-east-1.
# http://docs.aws.amazon.com/general/latest/gr/rande.html#r53_region
_REGISTRATION_ENDPOINT = "https://route53domains.us-east-1.amazonaws.com/"
_OTHER_ENDPOINT = "https://route53.amazonaws.com/"

_NS = "https://route53.amazonaws.com/doc/2013-04-01/"

class Route53Error(AWSError):
    pass


def route53_error_wrapper(error):
    error_wrapper(error, Route53Error)


def get_route53_client(agent, region, cooperator=None):
    """
    Get a non-registration Route53 client.
    """
    if cooperator is None:
        from twisted.internet import task as cooperator
    return region.get_client(
        _Route53Client,
        agent=agent,
        creds=region.creds,
        region=REGION_US_EAST_1,
        endpoint=AWSServiceEndpoint(_OTHER_ENDPOINT),
        cooperator=cooperator,
    )


RECORD_TYPES = {
    u"SOA": SOA,
    u"NS": NS,
    u"CNAME": CNAME,
}


@attr.s(frozen=True)
class _Route53Client(object):
    agent = attr.ib()
    creds = attr.ib()
    region = attr.ib()
    endpoint = attr.ib()
    cooperator = attr.ib()

    def _details(self, op):
        content_sha256 = sha256(op.body).hexdigest().decode("ascii")
        body_producer = FileBodyProducer(
            BytesIO(op.body), cooperator=self.cooperator,
        )
        return RequestDetails(
            region=self.region,
            service=op.service,
            method=op.method,
            url_context=url_context(
                scheme=self.endpoint.scheme.decode("ascii"),
                host=self.endpoint.host.decode("ascii"),
                port=self.endpoint.port,
                path=op.path,
                query=op.query,
            ),
            body_producer=body_producer,
            content_sha256=content_sha256,
        )

    def _submit(self, details, ok_status):
        q = query(credentials=self.creds, details=details, ok_status=ok_status)
        d = q.submit(self.agent)
        d.addErrback(route53_error_wrapper)
        d.addCallback(itemgetter(1))
        d.addCallback(XML)
        return d

    def _op(self, op):
        details = self._details(op)
        d = self._submit(details=details, ok_status=op.ok_status)
        d.addCallback(op.extract_result)
        return d

    def create_hosted_zone(self, caller_reference, name):
        """
        http://docs.aws.amazon.com/Route53/latest/APIReference/API_CreateHostedZone.html
        """
        d = _route53_op(
            method=b"POST",
            path=[u"2013-04-01", u"hostedzone"],
            body=tags.CreateHostedZoneRequest(xmlns=_NS)(
                tags.CallerReference(caller_reference),
                tags.Name(name),
                ),
            ok_status=(CREATED,),
            extract_result=self._handle_create_hosted_zone_response,
        )
        d.addCallback(self._op)
        return d

    def _handle_create_hosted_zone_response(self, document):
        # XXX Could extract some additional stuff
        # http://docs.aws.amazon.com/Route53/latest/APIReference/API_CreateHostedZone.html#API_CreateHostedZone_ResponseSyntax
        zone = document.find("./HostedZone")
        return hostedzone_from_element(zone)

    def list_hosted_zones(self):
        """
        http://docs.aws.amazon.com/Route53/latest/APIReference/API_ListHostedZones.html
        """
        d = _route53_op(
            method=b"GET",
            path=[u"2013-04-01", u"hostedzone"],
            extract_result=self._handle_list_hosted_zones_response,
        )
        d.addCallback(self._op)
        return d

    def _handle_list_hosted_zones_response(self, document):
        result = []
        hosted_zones = document.iterfind("./HostedZones/HostedZone")
        for zone in hosted_zones:
            result.append(hostedzone_from_element(zone))
        return result

    def change_resource_record_sets(self, zone_id, changes):
        """
        http://docs.aws.amazon.com/Route53/latest/APIReference/API_ChangeResourceRecordSets.html
        """
        d = _route53_op(
            method=b"POST",
            path=[u"2013-04-01", u"hostedzone", unicode(zone_id), u"rrset"],
            body=tags.ChangeResourceRecordSetsRequest(xmlns=_NS)(
                tags.ChangeBatch(
                    tags.Changes(list(
                        change.to_element()
                        for change in changes
                    ))
                )
            ),
        )
        d.addCallback(self._op)
        return d

    def list_resource_record_sets(self, zone_id, identifier=None, maxitems=None, name=None, type=None):
        """
        http://docs.aws.amazon.com/Route53/latest/APIReference/API_ListResourceRecordSets.html
        """
        args = []
        if identifier:
            args.append(("identifier", identifier))
        if maxitems:
            args.append(("maxitems", str(maxitems)))
        if name:
            args.append(("name", name))
        if type:
            args.append(("type", type))

        d = _route53_op(
            method=b"GET",
            path=[u"2013-04-01", u"hostedzone", unicode(zone_id), u"rrset"],
            query=args,
            extract_result=self._handle_list_resource_record_sets_response
        )
        d.addCallback(self._op)
        return d

    def _handle_list_resource_record_sets_response(self, document):
        result = {}
        rrsets = document.iterfind("./ResourceRecordSets/ResourceRecordSet")
        for rrset in rrsets:
            name = Name(maybe_bytes_to_unicode(rrset.find("Name").text))
            type = rrset.find("Type").text
            records = rrset.iterfind("./ResourceRecords/ResourceRecord")
            result.setdefault(name, set()).update({
                RECORD_TYPES[type].from_element(element)
                for element
                in records
            })
        return result


    def delete_hosted_zone(self, zone_id):
        """
        http://docs.aws.amazon.com/Route53/latest/APIReference/API_DeleteHostedZone.html
        """
        d = _route53_op(
            method=b"DELETE",
            path=[u"2013-04-01", u"hostedzone", unicode(zone_id)],
        )
        d.addCallback(self._op)
        return d

def _route53_op(body=None, **kw):
    op = _Op(service=b"route53", **kw)
    if body is None:
        return succeed(op)
    d = to_xml(body)
    d.addCallback(lambda body: attr.assoc(op, body=body))
    return d

@attr.s
class _Op(object):
    service = attr.ib()
    method = attr.ib()
    path = attr.ib()
    query = attr.ib(default=attr.Factory(list))
    body = attr.ib(default=b"")
    ok_status = attr.ib(default=(OK,))
    extract_result = attr.ib(default=lambda document: None)


def annotate_request_uri(uri):
    def annotate(reason):
        # Hard to make a copy of a Failure with only minor changes.
        # In particular, there's no way to be sure to replicate the
        # traceback.  Failure.cleanFailure() may have thrown the real
        # traceback object and the fake stuff that it gets replaced
        # with isn't acceptable to Failure.__init__.  So ... mutate
        # this one in place.  What could go wrong?  XXX TODO Replace
        # this with Eliot!
        reason.value = Exception("while requesting", uri, reason.value)
        reason.type = Exception
        return reason
    return annotate


@attr.s(frozen=True)
class _DeleteHostedZone(object):
    ok_status = (OK,)
    zone_id = attr.ib()

    method = b"DELETE"
    service = b"route53"

    def path(self):
        return [u"2013-04-01", u"hostedzone", unicode(self.zone_id)]

    def query(self):
        pass

    def xml_request_body(self):
        return None

    def extract_result(self, document):
        return None


def hostedzone_from_element(zone):
    return HostedZone(
        name=maybe_bytes_to_unicode(zone.find("Name").text),
        identifier=maybe_bytes_to_unicode(zone.find("Id").text).replace(u"/hostedzone/", u""),
        rrset_count=int(zone.find("ResourceRecordSetCount").text),
        reference=maybe_bytes_to_unicode(zone.find("CallerReference").text),
    )


@attr.s(frozen=True)
class _ChangeRRSet(object):
    action = attr.ib()
    name = attr.ib(validator=validators.instance_of(Name))
    type = attr.ib()
    rrset = attr.ib()

    def to_element(self):
        return tags.Change(
            tags.Action(
                self.action,
            ),
            tags.ResourceRecordSet(
                tags.Name(
                    unicode(self.name),
                ),
                tags.Type(
                    unicode(self.type),
                ),
                tags.TTL(
                    unicode(60 * 60 * 24),
                ),
                tags.ResourceRecords(list(
                    tags.ResourceRecord(tags.Value(rr.to_string()))
                    for rr
                    in self.rrset
                ))
            ),
        )

def create_rrset(name, type, rrset):
    return _ChangeRRSet(u"CREATE", name, type, rrset)


def upsert_rrset(name, type, rrset):
    pass


def delete_rrset(name, type, rrset):
    return _ChangeRRSet(u"DELETE", name, type, rrset)


def create_alias_rrset(name, type, alias):
    pass


def create_failover_rrset(name, type, failover):
    pass


def create_geolocation_rrset(name, type, geolocation):
    pass


def create_latency_based_rrset(name, type, latency):
    pass
