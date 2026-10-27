import logging

# The level is left to the `logger:` section of configuration.yaml. It used to
# be forced to INFO here, which silently ignored `custom_components.haier_evo: debug`.
_LOGGER = logging.getLogger("custom_components.haier_evo")
