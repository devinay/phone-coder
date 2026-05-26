# Architecture Plan

## Problem Statement

We need to transition from a monolithic architecture to a microservices architecture. The monolith currently uses a relational database management system (RDBMS), and the goal is to split this into three individual services, each utilizing its own key-value store.

## Proposed Architecture

![Architecture Diagram](./monolith_to_microservices.png)

1. **Monolithic System**
   - Contains the RDBMS.
   - Acts as the central hub from which data and processes are split.

2. **Service 1**
   - Utilizes its own key-value store.
   - Connects with the monolith for specific tasks.

3. **Service 2**
   - Utilizes its own key-value store.
   - Maintains integration for outlined functionalities.

4. **Service 3**
   - Utilizes its own key-value store.
   - Ensures robustness in handling dedicated tasks.

## Interaction

- Arrows indicate the flow of data from the monolith to each service, highlighting the transition strategy.

## Notes

- The key transformation step involves delineating shared functions into independent services while maintaining system integrity.
- Focus on ensuring compatibility and minimal downtime during the migration phase.

---
