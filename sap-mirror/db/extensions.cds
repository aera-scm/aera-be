// Mirror-specific additions to the S/4HANA schema (SRD 6.6.2). Everything here is a
// declared extension, not an S/4HANA field.
using { aera.mirror.s4 as s4 } from './s4';

namespace aera.mirror;

// BR-06: compliance gate on supplier master.
extend s4.A_Supplier with {
  ComplianceStatus : String(20) @assert.range enum { APPROVED; UNDER_REVIEW; BLOCKED; };
}

// S/4HANA keeps change stamps the Mirror's schema subset does not carry. These technical
// ETag elements give the entities that AERA updates optimistic locking with If-Match.
annotate s4.A_PurchaseOrder with {
  LastChangeDateTime @odata.etag @cds.on.insert: $now @cds.on.update: $now;
}

extend s4.A_PurchaseOrderScheduleLine with {
  MirrorETag : Timestamp @odata.etag @cds.on.insert: $now @cds.on.update: $now;
}

// MRP exception feed (OI-07): not exposed by the listed S/4HANA APIs.
entity MRPExceptionMessage {
  key MRPExceptionMessageID : String(10);
  Material                  : String(40);
  Plant                     : String(4);
  MRPElement                : String(10);
  MRPElementItem            : String(5);
  MRPExceptionNumber        : String(2);
  MRPExceptionText          : String(60);
  MRPElementDate            : Date;
  MRPReschedulingDate       : Date;
  CreationDateTime          : Timestamp;
}

// Consumption per hour; in production derived from production order components.
entity MaterialConsumptionRate {
  key Material               : String(40);
  key Plant                  : String(4);
  ConsumptionQuantityPerHour : Decimal(13, 3);
  MaterialBaseUnit           : String(3);
}
