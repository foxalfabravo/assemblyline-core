"""
This object encapsulates the

"""
import re
import logging
from assemblyline.common.forge import CachedObject


# def normalize_data(data):
#     if isinstance(data, dict):
#         return tuple((k, normalize_data(data[k])) for k in sorted(data.keys()))
#     elif isinstance(data, (list, tuple)):
#         return tuple(normalize_data(v) for v in data)
#     else:
#         return data
#
#
# def config_hash(config):
#     return str(hash(normalize_data(config)))
#


class Scheduler:
    system_category = 'system'

    def __init__(self, datastore, config):
        self.datastore = datastore
        self.config = config
        self.services = CachedObject(self._get_services)


    def build_schedule(self, submission, file_type: str):
        all_services = dict(self.services.items())

        # Load the selected and excluded services by category
        excluded = self.expand_categories(submission.params.services.excluded)
        if not submission.params.services.selected:
            selected = [s for s in all_services.keys()]
        else:
            selected = self.expand_categories(submission.params.services.selected)

        # Add in all system services, they are always needed, and can't be excluded
        system_services = [k for k, v in all_services.items() if v.category == self.system_category]

        # Add all selected, accepted, and not rejected services to the schedule
        schedule = [{} for _ in self.stages()]
        services = list((set(selected) - set(excluded)) | set(system_services))
        selected = []
        skipped = []
        for name in services:
            service = all_services.get(name, None)

            if not service:
                skipped.append(name)
                logging.warning(f"Service configuration not found: {name}")
                continue

            accepted = not service.accepts or re.match(service.accepts, file_type)
            rejected = bool(service.rejects) and re.match(service.rejects, file_type)

            if accepted and not rejected:
                schedule[self.stage_index(service.stage)][name] = service
                selected.append(name)
            else:
                skipped.append(name)

        return schedule

    def expand_categories(self, services: list):
        """Expands the names of service categories found in the list of services.

        Args:
            services (list): List of service catagory or service names.
        """
        if services is None:
            return []

        services = list(services)
        categories = self.categories()

        found_services = []
        seen_categories = set()
        while services:
            name = services.pop()

            # If we found a new category mix in it's content
            if name in categories:
                if name not in seen_categories:
                    # Add all of the items in this group to the list of
                    # things that we need to evaluate, and mark this
                    # group as having been seen.
                    services.extend(categories[name])
                    seen_categories.update(name)
                continue

            # If it isn't a category, its a service
            found_services.append(name)

        # Use set to remove duplicates, set is more efficent in batches
        return list(set(found_services))

    def categories(self):
        all_categories = {}
        for service in self.services.values():
            try:
                all_categories[service.category].append(service.name)
            except KeyError:
                all_categories[service.category] = [service.name]
        return all_categories

    def stage_index(self, stage):
        return self.stages().index(stage)

    def stages(self):
        return self.config.core.dispatcher.stages

    def _get_services(self):
        return {ser.name: ser for ser in self.datastore.service.search('*:*', fl='*', filter='enabled: True', rows=1000)['items']}

    # def build_service_config(self, service_name, submission):
    #     """
    #     Determine the parameter mapping for a service.
    #
    #     Combine the default and submission specific service parameters to
    #     produce the final configuration for this submission.
    #     """
    #     params = dict(self.services[service_name].params)
    #     params.update(submission.params.service_spec.get(service_name, {}))
    #     return params