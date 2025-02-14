import itertools
from collections import defaultdict
from datetime import datetime
from functools import reduce
from itertools import groupby
from operator import itemgetter
from typing import (
    Sequence,
    Optional,
    Type,
    Tuple,
    Dict,
    Set,
    TypeVar,
    Callable,
    Mapping,
)

from mongoengine import Q, Document

from apiserver import database
from apiserver.apierrors import errors
from apiserver.config_repo import config
from apiserver.database.model import EntityVisibility, AttributedDocument
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import Task, TaskStatus, external_task_types
from apiserver.database.utils import get_options, get_company_or_none_constraint
from apiserver.timing_context import TimingContext
from apiserver.utilities.dicts import nested_get
from .sub_projects import (
    _reposition_project_with_children,
    _ensure_project,
    _validate_project_name,
    _update_subproject_names,
    _save_under_parent,
    _get_sub_projects,
    _ids_with_children,
    _ids_with_parents,
    _get_project_depth,
)

log = config.logger(__file__)
max_depth = config.get("services.projects.sub_projects.max_depth", 10)


class ProjectBLL:
    @classmethod
    def merge_project(
        cls, company, source_id: str, destination_id: str
    ) -> Tuple[int, int, Set[str]]:
        """
        Move all the tasks and sub projects from the source project to the destination
        Remove the source project
        Return the amounts of moved entities and subprojects + set of all the affected project ids
        """
        with TimingContext("mongo", "move_project"):
            if source_id == destination_id:
                raise errors.bad_request.ProjectSourceAndDestinationAreTheSame(
                    parent=source_id
                )
            source = Project.get(company, source_id)
            destination = Project.get(company, destination_id)

            children = _get_sub_projects(
                [source.id], _only=("id", "name", "parent", "path")
            )[source.id]
            cls.validate_projects_depth(
                projects=children,
                old_parent_depth=len(source.path) + 1,
                new_parent_depth=len(destination.path) + 1,
            )

            moved_entities = 0
            for entity_type in (Task, Model):
                moved_entities += entity_type.objects(
                    company=company,
                    project=source_id,
                    system_tags__nin=[EntityVisibility.archived.value],
                ).update(upsert=False, project=destination_id)

            moved_sub_projects = 0
            for child in Project.objects(company=company, parent=source_id):
                _reposition_project_with_children(
                    project=child,
                    children=[c for c in children if c.parent == child.id],
                    parent=destination,
                )
                moved_sub_projects += 1

            affected = {source.id, *(source.path or [])}
            source.delete()

            if destination:
                destination.update(last_update=datetime.utcnow())
                affected.update({destination.id, *(destination.path or [])})

        return moved_entities, moved_sub_projects, affected

    @staticmethod
    def validate_projects_depth(
        projects: Sequence[Project], old_parent_depth: int, new_parent_depth: int
    ):
        for current in projects:
            current_depth = len(current.path) + 1
            if current_depth - old_parent_depth + new_parent_depth > max_depth:
                raise errors.bad_request.ProjectPathExceedsMax(max_depth=max_depth)

    @classmethod
    def move_project(
        cls, company: str, user: str, project_id: str, new_location: str
    ) -> Tuple[int, Set[str]]:
        """
        Move project with its sub projects from its current location to the target one.
        If the target location does not exist then it will be created. If it exists then
        it should be writable. The source location should be writable too.
        Return the number of moved projects + set of all the affected project ids
        """
        with TimingContext("mongo", "move_project"):
            project = Project.get(company, project_id)
            old_parent_id = project.parent
            old_parent = (
                Project.get_for_writing(company=project.company, id=old_parent_id)
                if old_parent_id
                else None
            )

            children = _get_sub_projects([project.id], _only=("id", "name", "path"))[
                project.id
            ]
            cls.validate_projects_depth(
                projects=[project, *children],
                old_parent_depth=len(project.path),
                new_parent_depth=_get_project_depth(new_location),
            )

            new_parent = _ensure_project(company=company, user=user, name=new_location)
            new_parent_id = new_parent.id if new_parent else None
            if old_parent_id == new_parent_id:
                raise errors.bad_request.ProjectSourceAndDestinationAreTheSame(
                    location=new_parent.name if new_parent else ""
                )

            moved = _reposition_project_with_children(
                project, children=children, parent=new_parent
            )

            now = datetime.utcnow()
            affected = set()
            for p in filter(None, (old_parent, new_parent)):
                p.update(last_update=now)
                affected.update({p.id, *(p.path or [])})

            return moved, affected

    @classmethod
    def update(cls, company: str, project_id: str, **fields):
        with TimingContext("mongo", "projects_update"):
            project = Project.get_for_writing(company=company, id=project_id)
            if not project:
                raise errors.bad_request.InvalidProjectId(id=project_id)

            new_name = fields.pop("name", None)
            if new_name:
                new_name, new_location = _validate_project_name(new_name)
                old_name, old_location = _validate_project_name(project.name)
                if new_location != old_location:
                    raise errors.bad_request.CannotUpdateProjectLocation(name=new_name)
                fields["name"] = new_name

            fields["last_update"] = datetime.utcnow()
            updated = project.update(upsert=False, **fields)

            if new_name:
                old_name = project.name
                project.name = new_name
                children = _get_sub_projects(
                    [project.id], _only=("id", "name", "path")
                )[project.id]
                _update_subproject_names(
                    project=project, children=children, old_name=old_name
                )

            return updated

    @classmethod
    def create(
        cls,
        user: str,
        company: str,
        name: str,
        description: str = "",
        tags: Sequence[str] = None,
        system_tags: Sequence[str] = None,
        default_output_destination: str = None,
    ) -> str:
        """
        Create a new project.
        Returns project ID
        """
        if _get_project_depth(name) > max_depth:
            raise errors.bad_request.ProjectPathExceedsMax(max_depth=max_depth)

        name, location = _validate_project_name(name)
        now = datetime.utcnow()
        project = Project(
            id=database.utils.id(),
            user=user,
            company=company,
            name=name,
            description=description,
            tags=tags,
            system_tags=system_tags,
            default_output_destination=default_output_destination,
            created=now,
            last_update=now,
        )
        parent = _ensure_project(company=company, user=user, name=location)
        _save_under_parent(project=project, parent=parent)
        if parent:
            parent.update(last_update=now)

        return project.id

    @classmethod
    def find_or_create(
        cls,
        user: str,
        company: str,
        project_name: str,
        description: str,
        project_id: str = None,
        tags: Sequence[str] = None,
        system_tags: Sequence[str] = None,
        default_output_destination: str = None,
    ) -> str:
        """
        Find a project named `project_name` or create a new one.
        Returns project ID
        """
        if not project_id and not project_name:
            raise ValueError("project id or name required")

        if project_id:
            project = Project.objects(company=company, id=project_id).only("id").first()
            if not project:
                raise errors.bad_request.InvalidProjectId(id=project_id)
            return project_id

        project_name, _ = _validate_project_name(project_name)
        project = Project.objects(company=company, name=project_name).only("id").first()
        if project:
            return project.id

        return cls.create(
            user=user,
            company=company,
            name=project_name,
            description=description,
            tags=tags,
            system_tags=system_tags,
            default_output_destination=default_output_destination,
        )

    @classmethod
    def move_under_project(
        cls,
        entity_cls: Type[Document],
        user: str,
        company: str,
        ids: Sequence[str],
        project: str = None,
        project_name: str = None,
    ):
        """
        Move a batch of entities to `project` or a project named `project_name` (create if does not exist)
        """
        with TimingContext("mongo", "move_under_project"):
            project = cls.find_or_create(
                user=user,
                company=company,
                project_id=project,
                project_name=project_name,
                description="",
            )
            extra = (
                {"set__last_change": datetime.utcnow()}
                if hasattr(entity_cls, "last_change")
                else {}
            )
            entity_cls.objects(company=company, id__in=ids).update(
                set__project=project, **extra
            )

            return project

    archived_tasks_cond = {"$in": [EntityVisibility.archived.value, "$system_tags"]}

    @classmethod
    def make_projects_get_all_pipelines(
        cls,
        company_id: str,
        project_ids: Sequence[str],
        specific_state: Optional[EntityVisibility] = None,
    ) -> Tuple[Sequence, Sequence]:
        archived = EntityVisibility.archived.value

        def ensure_valid_fields():
            """
            Make sure system tags is always an array (required by subsequent $in in archived_tasks_cond
            """
            return {
                "$addFields": {
                    "system_tags": {
                        "$cond": {
                            "if": {"$ne": [{"$type": "$system_tags"}, "array"]},
                            "then": [],
                            "else": "$system_tags",
                        }
                    },
                    "status": {"$ifNull": ["$status", "unknown"]},
                }
            }

        status_count_pipeline = [
            # count tasks per project per status
            {
                "$match": {
                    "company": {"$in": [None, "", company_id]},
                    "project": {"$in": project_ids},
                }
            },
            ensure_valid_fields(),
            {
                "$group": {
                    "_id": {
                        "project": "$project",
                        "status": "$status",
                        archived: cls.archived_tasks_cond,
                    },
                    "count": {"$sum": 1},
                }
            },
            # for each project, create a list of (status, count, archived)
            {
                "$group": {
                    "_id": "$_id.project",
                    "counts": {
                        "$push": {
                            "status": "$_id.status",
                            "count": "$count",
                            archived: "$_id.%s" % archived,
                        }
                    },
                }
            },
        ]

        def runtime_subquery(additional_cond):
            return {
                # the sum of
                "$sum": {
                    # for each task
                    "$cond": {
                        # if completed and started and completed > started
                        "if": {
                            "$and": [
                                "$started",
                                "$completed",
                                {"$gt": ["$completed", "$started"]},
                                additional_cond,
                            ]
                        },
                        # then: floor((completed - started) / 1000)
                        "then": {
                            "$floor": {
                                "$divide": [
                                    {"$subtract": ["$completed", "$started"]},
                                    1000.0,
                                ]
                            }
                        },
                        "else": 0,
                    }
                }
            }

        group_step = {"_id": "$project"}

        for state in EntityVisibility:
            if specific_state and state != specific_state:
                continue
            if state == EntityVisibility.active:
                group_step[state.value] = runtime_subquery(
                    {"$not": cls.archived_tasks_cond}
                )
            elif state == EntityVisibility.archived:
                group_step[state.value] = runtime_subquery(cls.archived_tasks_cond)

        runtime_pipeline = [
            # only count run time for these types of tasks
            {
                "$match": {
                    "company": {"$in": [None, "", company_id]},
                    "type": {"$in": ["training", "testing", "annotation"]},
                    "project": {"$in": project_ids},
                }
            },
            ensure_valid_fields(),
            {
                # for each project
                "$group": group_step
            },
        ]

        return status_count_pipeline, runtime_pipeline

    T = TypeVar("T")

    @staticmethod
    def aggregate_project_data(
        func: Callable[[T, T], T],
        project_ids: Sequence[str],
        child_projects: Mapping[str, Sequence[Project]],
        data: Mapping[str, T],
    ) -> Dict[str, T]:
        """
        Given a list of project ids and data collected over these projects and their subprojects
        For each project aggregates the data from all of its subprojects
        """
        aggregated = {}
        if not data:
            return aggregated
        for pid in project_ids:
            relevant_projects = {p.id for p in child_projects.get(pid, [])} | {pid}
            relevant_data = [data for p, data in data.items() if p in relevant_projects]
            if not relevant_data:
                continue
            aggregated[pid] = reduce(func, relevant_data)
        return aggregated

    @classmethod
    def get_project_stats(
        cls,
        company: str,
        project_ids: Sequence[str],
        specific_state: Optional[EntityVisibility] = None,
    ) -> Tuple[Dict[str, dict], Dict[str, dict]]:
        if not project_ids:
            return {}, {}

        child_projects = _get_sub_projects(project_ids, _only=("id", "name"))
        project_ids_with_children = set(project_ids) | {
            c.id for c in itertools.chain.from_iterable(child_projects.values())
        }
        status_count_pipeline, runtime_pipeline = cls.make_projects_get_all_pipelines(
            company,
            project_ids=list(project_ids_with_children),
            specific_state=specific_state,
        )

        default_counts = dict.fromkeys(get_options(TaskStatus), 0)

        def set_default_count(entry):
            return dict(default_counts, **entry)

        status_count = defaultdict(lambda: {})
        key = itemgetter(EntityVisibility.archived.value)
        for result in Task.aggregate(status_count_pipeline):
            for k, group in groupby(sorted(result["counts"], key=key), key):
                section = (
                    EntityVisibility.archived if k else EntityVisibility.active
                ).value
                status_count[result["_id"]][section] = set_default_count(
                    {
                        count_entry["status"]: count_entry["count"]
                        for count_entry in group
                    }
                )

        def sum_status_count(
            a: Mapping[str, Mapping], b: Mapping[str, Mapping]
        ) -> Dict[str, dict]:
            return {
                section: {
                    status: nested_get(a, (section, status), 0)
                    + nested_get(b, (section, status), 0)
                    for status in set(a.get(section, {})) | set(b.get(section, {}))
                }
                for section in set(a) | set(b)
            }

        status_count = cls.aggregate_project_data(
            func=sum_status_count,
            project_ids=project_ids,
            child_projects=child_projects,
            data=status_count,
        )

        runtime = {
            result["_id"]: {k: v for k, v in result.items() if k != "_id"}
            for result in Task.aggregate(runtime_pipeline)
        }

        def sum_runtime(
            a: Mapping[str, Mapping], b: Mapping[str, Mapping]
        ) -> Dict[str, dict]:
            return {
                section: a.get(section, 0) + b.get(section, 0)
                for section in set(a) | set(b)
            }

        runtime = cls.aggregate_project_data(
            func=sum_runtime,
            project_ids=project_ids,
            child_projects=child_projects,
            data=runtime,
        )

        def get_status_counts(project_id, section):
            return {
                "total_runtime": nested_get(runtime, (project_id, section), 0),
                "status_count": nested_get(
                    status_count, (project_id, section), default_counts
                ),
            }

        report_for_states = [
            s for s in EntityVisibility if not specific_state or specific_state == s
        ]

        stats = {
            project: {
                task_state.value: get_status_counts(project, task_state.value)
                for task_state in report_for_states
            }
            for project in project_ids
        }

        children = {
            project: sorted(
                [{"id": c.id, "name": c.name} for c in child_projects.get(project, [])],
                key=itemgetter("name"),
            )
            for project in project_ids
        }
        return stats, children

    @classmethod
    def get_active_users(
        cls,
        company,
        project_ids: Sequence[str],
        user_ids: Optional[Sequence[str]] = None,
    ) -> Set[str]:
        """
        Get the set of user ids that created tasks/models in the given projects
        If project_ids is empty then all projects are examined
        If user_ids are passed then only subset of these users is returned
        """
        with TimingContext("mongo", "active_users_in_projects"):
            query = Q(company=company)
            if user_ids:
                query &= Q(user__in=user_ids)

            projects_query = query
            if project_ids:
                project_ids = _ids_with_children(project_ids)
                query &= Q(project__in=project_ids)
                projects_query &= Q(id__in=project_ids)

            res = set(Project.objects(projects_query).distinct(field="user"))
            for cls_ in (Task, Model):
                res |= set(cls_.objects(query).distinct(field="user"))

            return res

    @classmethod
    def get_projects_with_active_user(
        cls,
        company: str,
        users: Sequence[str],
        project_ids: Optional[Sequence[str]] = None,
        allow_public: bool = True,
    ) -> Sequence[str]:
        """
        Get the projects ids where user created any tasks including all the parents of these projects
        If project ids are specified then filter the results by these project ids
        """
        query = Q(user__in=users)

        if allow_public:
            query &= get_company_or_none_constraint(company)
        else:
            query &= Q(company=company)

        user_projects_query = query
        if project_ids:
            ids_with_children = _ids_with_children(project_ids)
            query &= Q(project__in=ids_with_children)
            user_projects_query &= Q(id__in=ids_with_children)

        res = {p.id for p in Project.objects(user_projects_query).only("id")}
        for cls_ in (Task, Model):
            res |= set(cls_.objects(query).distinct(field="project"))

        res = list(res)
        if not res:
            return res

        ids_with_parents = _ids_with_parents(res)
        if project_ids:
            return [pid for pid in ids_with_parents if pid in project_ids]

        return ids_with_parents

    @classmethod
    def get_task_parents(
        cls,
        company_id: str,
        projects: Sequence[str],
        include_subprojects: bool,
        state: Optional[EntityVisibility] = None,
    ) -> Sequence[dict]:
        """
        Get list of unique parent tasks sorted by task name for the passed company projects
        If projects is None or empty then get parents for all the company tasks
        """
        query = Q(company=company_id)
        if projects:
            if include_subprojects:
                projects = _ids_with_children(projects)
            query &= Q(project__in=projects)
        if state == EntityVisibility.archived:
            query &= Q(system_tags__in=[EntityVisibility.archived.value])
        elif state == EntityVisibility.active:
            query &= Q(system_tags__nin=[EntityVisibility.archived.value])

        parent_ids = set(Task.objects(query).distinct("parent"))
        if not parent_ids:
            return []

        parents = Task.get_many_with_join(
            company_id,
            query=Q(id__in=parent_ids),
            allow_public=True,
            override_projection=("id", "name", "project.name"),
        )
        return sorted(parents, key=itemgetter("name"))

    @classmethod
    def get_task_types(cls, company, project_ids: Optional[Sequence]) -> set:
        """
        Return the list of unique task types used by company and public tasks
        If project ids passed then only tasks from these projects are considered
        """
        query = get_company_or_none_constraint(company)
        if project_ids:
            project_ids = _ids_with_children(project_ids)
            query &= Q(project__in=project_ids)
        res = Task.objects(query).distinct(field="type")
        return set(res).intersection(external_task_types)

    @classmethod
    def get_model_frameworks(cls, company, project_ids: Optional[Sequence]) -> Sequence:
        """
        Return the list of unique frameworks used by company and public models
        If project ids passed then only models from these projects are considered
        """
        query = get_company_or_none_constraint(company)
        if project_ids:
            project_ids = _ids_with_children(project_ids)
            query &= Q(project__in=project_ids)
        return Model.objects(query).distinct(field="framework")

    @classmethod
    def calc_own_contents(cls, company: str, project_ids: Sequence[str]) -> Dict[str, dict]:
        """
        Returns the amount of task/models per requested project
        Use separate aggregation calls on Task/Model instead of lookup
        aggregation on projects in order not to hit memory limits on large tasks
        """
        if not project_ids:
            return {}

        pipeline = [
            {
                "$match": {
                    "company": {"$in": [None, "", company]},
                    "project": {"$in": project_ids},
                }
            },
            {
                "$project": {"project": 1}
            },
            {
                "$group": {
                    "_id": "$project",
                    "count": {"$sum": 1},
                }
            }
        ]

        def get_agrregate_res(cls_: Type[AttributedDocument]) -> dict:
            return {
                data["_id"]: data["count"]
                for data in cls_.aggregate(pipeline)
            }

        with TimingContext("mongo", "get_security_groups"):
            tasks = get_agrregate_res(Task)
            models = get_agrregate_res(Model)
            return {
                pid: {
                    "own_tasks": tasks.get(pid, 0),
                    "own_models": models.get(pid, 0),
                }
                for pid in project_ids
            }
