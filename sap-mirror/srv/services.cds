// OData services with the S/4HANA API names (SRD 6.6, IR-02). The V2 adapter serves them
// under /sap/opu/odata/sap/<service>, the same path layout as an S/4HANA tenant (IR-03).
using { aera.mirror.s4 as s4 } from '../db/s4';
using { aera.mirror as m } from '../db/extensions';

@path: 'API_PURCHASEORDER_PROCESS_SRV'
@requires: 'authenticated-user'
service API_PURCHASEORDER_PROCESS_SRV {
  entity A_PurchaseOrder             as projection on s4.A_PurchaseOrder;
  entity A_PurchaseOrderItem         as projection on s4.A_PurchaseOrderItem;
  entity A_PurchaseOrderScheduleLine as projection on s4.A_PurchaseOrderScheduleLine;
}

@path: 'API_MATERIAL_STOCK_SRV'
@requires: 'authenticated-user'
service API_MATERIAL_STOCK_SRV {
  @readonly entity A_MatlStkInAcctMod as projection on s4.A_MatlStkInAcctMod;
}

@path: 'API_SALES_ORDER_SRV'
@requires: 'authenticated-user'
service API_SALES_ORDER_SRV {
  @readonly entity A_SalesOrder             as projection on s4.A_SalesOrder;
  @readonly entity A_SalesOrderItem         as projection on s4.A_SalesOrderItem;
  @readonly entity A_SalesOrderScheduleLine as projection on s4.A_SalesOrderScheduleLine;
}

@path: 'API_PRODUCTION_ORDER_2_SRV'
@requires: 'authenticated-user'
service API_PRODUCTION_ORDER_2_SRV {
  @readonly entity A_ProductionOrder_2 as projection on s4.A_ProductionOrder_2;
  @readonly entity A_ProductionOrderComponent_2 as projection on s4.A_ProductionOrderComponent_2;
}

@path: 'API_BUSINESS_PARTNER'
@requires: 'authenticated-user'
service API_BUSINESS_PARTNER {
  @readonly entity A_BusinessPartner        as projection on s4.A_BusinessPartner;
  @readonly entity A_Supplier               as projection on s4.A_Supplier;
  @readonly entity A_BusinessPartnerAddress as projection on s4.A_BusinessPartnerAddress;
  @readonly entity A_AddressEmailAddress    as projection on s4.A_AddressEmailAddress;
  @readonly entity A_AddressPhoneNumber     as projection on s4.A_AddressPhoneNumber;
}

// GET for AERA; POST simulates goods arrival (SRD 6.6.2).
@path: 'API_MATERIAL_DOCUMENT_SRV'
@requires: 'authenticated-user'
service API_MATERIAL_DOCUMENT_SRV {
  entity A_MaterialDocumentItem as projection on s4.A_MaterialDocumentItem;
}

// Customer-namespace service for Mirror-only entities (OI-07).
@path: 'ZAERA_MIRROR_SRV'
@requires: 'authenticated-user'
service ZAERA_MIRROR_SRV {
  @readonly entity MRPExceptionMessage     as projection on m.MRPExceptionMessage;
  @readonly entity MaterialConsumptionRate as projection on m.MaterialConsumptionRate;
}

// FR-ADM-03: restore the reference scenario relative to a scenario start T0.
@path: '/admin'
@requires: 'MirrorAdmin'
service MirrorAdminService {
  action reset(t0 : Timestamp) returns {
    t0         : Timestamp;
    rows       : Integer;
    durationMs : Integer;
  };
  // AT-08: fail the next `count` S/4 write requests with `status`, after letting `skip`
  // writes through (e.g. skip 1, count 4, status 500 = the second write fails, retries too).
  action fault(skip : Integer, count : Integer, status : Integer) returns {
    skip   : Integer;
    count  : Integer;
    status : Integer;
  };
}
