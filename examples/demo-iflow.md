# FICTIONAL DEMO: Customer replication iFlow

This document is invented example data for testing the local assistant. It does
not describe any real organization, SAP tenant, or production integration.

## Identity

- iFlow ID: DEMO_CUSTOMER_REPLICATION
- Package: Fictional Customer Integrations
- Environment: DEMO
- Documentation version: 1.0
- Owner: Example Integration Team

## Purpose

The fictional Customer Replication iFlow receives customer updates from an
example CRM and forwards them to an example ERP system. The incoming message
contains CustomerId, EventId, Name, CountryCode, and UpdatedAt.

## Mapping

CustomerId maps to BusinessPartner.ExternalId. Name maps to
BusinessPartner.FullName. CountryCode maps to BusinessPartner.Country.
UpdatedAt is parsed as an ISO 8601 timestamp. CountryCode is required; a missing
value causes validation to fail before the receiver call.

## Duplicate handling

In this fictional design, EventId is the idempotency key. A successfully
processed EventId is kept in a duplicate-check store for seven days. If the same
EventId arrives within that window, the flow skips the receiver call and records
the status DUPLICATE_SKIPPED. The duplicate check is based on EventId, not
CustomerId, so a new event for the same customer can still be processed.

## Retry behavior

An example receiver response of HTTP 429 or HTTP 503 triggers up to three
retries, separated by 30 seconds. A validation error or HTTP 400 is not retried.
After the retries are exhausted, the message is recorded as failed for manual
investigation by the Example Integration Team.

## Monitoring

The example correlation properties are EventId and CustomerId. The team first
checks the message processing log, the validation result, and the receiver
response. This document contains no live runtime status or real credentials.

## Questions to try

- How does DEMO_CUSTOMER_REPLICATION handle duplicate messages?
- Which field maps to BusinessPartner.Country?
- How many retries happen after HTTP 503?
- Who owns this fictional iFlow?
