This tool expects input data to be provided in a structured format that enables flexible and efficient processing. The schema defines the expected structure, types, and constraints for the input data, ensuring that it adheres to the required specifications. This is based on the following use cases:

## Scenarios

1. Creation flood inundation maps using the OWP HAND-FIM method for a single known river reach. For example, a researcher has a streamflow estimate which will be provided as input to for a single river. The output result should be a single GeoTiff. The output naming will defined by the scenario.

> Example input format (CSV)
>
> |ScenarioID|HUC|ReachID|Streamflow|
> |---|---|---|---|
> |my-scenario|12345678 | 9821264 | 300.1|

> Example input format (JSON)
>
> ```json
> [
>   {
>     "ScenarioID": "my-scenario",
>     "Reaches": [
>       { "HUC": "12345678", "ReachID": "9821264", "Streamflow": 300.1 }
>     ]
>   }
> ]
> ```

2. Creation of a single flood inundation map using the OWP HAND-FIM method for a scenario in which multiple rivers flood. In this scenario, a user provides multiple streamflow estimates as inputs (one per river) for a set of rivers. The output result is a single GeoTiff. The output naming will defined by the scenario.

> Example input format (CSV)
>
> |ScenarioID|HUC|ReachID|Streamflow|
> |---|---|---|---|
> |My-scenario|12345678|9821264|300.1|
> |My-scenario|12345678|9821263|300.7|
> |My-scenario|12345678|9821262|299.8|
> |My-scenario|12345678|9821261|288.0|
> |My-scenario|12345678|9821260|305.1|

> Example input format (JSON)
>
> ```json
> [
>   {
>     "ScenarioID": "My-scenario",
>     "Reaches": [
>       { "HUC": "12345678", "ReachID": "9821264", "Streamflow": 300.1 },
>       { "HUC": "12345678", "ReachID": "9821263", "Streamflow": 300.7 },
>       { "HUC": "12345678", "ReachID": "9821262", "Streamflow": 299.8 },
>       { "HUC": "12345678", "ReachID": "9821261", "Streamflow": 288.0 },
>       { "HUC": "12345678", "ReachID": "9821260", "Streamflow": 305.1 }
>     ]
>   }
> ]
> ```

3. Creation of multiple flood inundation maps using the OWP HAND-FIM method for one or more rivers over the duration of a hydrograph. In this scenario, a researcher provides one or more input streamflows for each rivers. Thge output result should be one map for each scenario.  The output naming will defined by the scenario.

> Example input format (CSV)
>
> |ScenarioID|HUC|ReachID|Streamflow|
> |---|---|---|---|
> |scenario1|12345678|9821264|300.1|
> |scenario1|12345678|9821263|300.7|
> |scenario1|12345678|9821262|299.8|
> |scenario1|12345678|9821261|288.0|
> |scenario1|12345678|9821260|305.1|
> |scenario2|12345678|9821264|310.1|
> |scenario2|12345678|9821263|301.7|
> |scenario2|12345678|9821262|297.8|
> |scenario2|12345678|9821261|298.0|
> |scenario2|12345678|9821260|315.1|

> Example input format (JSON)
>
> ```json
> [
>   {
>     "ScenarioID": "scenario1",
>     "Reaches": [
>       { "HUC": "12345678", "ReachID": "9821264", "Streamflow": 300.1 },
>       { "HUC": "12345678", "ReachID": "9821263", "Streamflow": 300.7 },
>       { "HUC": "12345678", "ReachID": "9821262", "Streamflow": 299.8 },
>       { "HUC": "12345678", "ReachID": "9821261", "Streamflow": 288.0 },
>       { "HUC": "12345678", "ReachID": "9821260", "Streamflow": 305.1 }
>     ]
>   },
>   {
>     "ScenarioID": "scenario2",
>     "Reaches": [
>       { "HUC": "12345678", "ReachID": "9821264", "Streamflow": 310.1 },
>       { "HUC": "12345678", "ReachID": "9821263", "Streamflow": 301.7 },
>       { "HUC": "12345678", "ReachID": "9821262", "Streamflow": 297.8 },
>       { "HUC": "12345678", "ReachID": "9821261", "Streamflow": 298.0 },
>       { "HUC": "12345678", "ReachID": "9821260", "Streamflow": 315.1 }
>     ]
>   }
> ]
> ```

## Terminology

* Scenario: A scenario is a specific set of input data and parameters that define a particular flood inundation mapping job. Each scenario will have its own unique identifier and associated input data.

* HUC: The Hydrologic Unit Code (HUC) used to identify the primary watershed corresponding to the input data. This is used to collect the necessary input HAND and rating curve datasets.

* ReachID: The NWM identifier for the river reach corresponding to the input data. This is used to process HAND-FIM for the location of interest.

* Streamflow: The streamflow value in cubic meters per second (cms) that is used as input for the HAND-FIM method. This value is critical for determining the extent of flooding in the inundation mapping process.
