from setuptools import setup

setup(
    name='upstream_wpt_webhook',
    version='0.1.0',
    author='The Servo Project Developers',
    url='https://github.com/servo-automation/upstream-wpt-sync-webhook/',
    description='A service that upstreams local changes to web-platform-tests',

    packages=['upstream_wpt_webhook'],
    install_requires=[
        'flask',
        'requests',
    ],
    entry_points={
        'console_scripts': [
            'upstream_wpt_webhook=upstream_wpt_webhook.flask_server:main',
        ],
    },
    zip_safe=False,
)
