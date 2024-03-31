import json
import os
import time

import requests
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import QuerySet, F
from django.shortcuts import get_object_or_404
from rest_framework import status, parsers
from rest_framework.filters import SearchFilter
from rest_framework.generics import CreateAPIView, RetrieveAPIView, RetrieveDestroyAPIView, ListCreateAPIView, \
    ListAPIView, DestroyAPIView
from rest_framework.pagination import CursorPagination
from rest_framework.permissions import IsAuthenticated, IsAuthenticatedOrReadOnly
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet

from nextnovel.exceptions import RequestAIServerError
from nextnovel.permissions import IsOwnerOrReadOnly
from nextnovel.settings import DEV
from nextnovel.throttles import LikeRateThrottle
from novels.models import NovelComment, Novel, NovelLike, Genre, NovelContent, NovelContentImage, NovelStats
from novels.serializers import NovelPreviewSerializer, \
    NovelCommentSerializer, NovelLikeSerializer, NovelListSerializer, NovelStartSerializer, NovelContinueSerializer, \
    NovelEndSerializer, NovelReadSerializer, NovelCoverImageSerializer, NovelContentQuestionSerializer, \
    NovelCompleteSerializer, NovelDetailSerializer, NovelContentSerializer, NovelImageSerializer
from users.models import User

url = os.environ.get("AI_URL", "http://j8a502.p.ssafy.io:8001/")

start_url = url + "novel/start"
question_url = url + "novel/question"
sequence_url = url + "novel/sequence"
end_url = url + "novel/end"
image_url = url + "novel/image"


def retrieve_question_from_ai_json(dialog_history):
    data = {
        "dialog_history": json.dumps(dialog_history)
    }

    response = requests.post(question_url, data=data)
    if response.status_code != 200:
        raise RequestAIServerError
    return response.json()


def novel_content_with_query(response, novel_content):
    query1 = response.get("query1")
    query2 = response.get("query2")
    query3 = response.get("query3")
    novel_content.query1 = query1
    novel_content.query2 = query2
    novel_content.query3 = query3
    return novel_content


def get_next_novel_content(novel_content, novel):
    step = novel_content.step
    step += 1
    return NovelContent.objects.create(step=step, novel=novel)


class NovelRecAPI(ListAPIView):
    queryset = Novel.objects.all().filter(status=Novel.Status.FINISHED)
    serializer_class = NovelPreviewSerializer

    def get_queryset(self):
        queryset = self.queryset.all().select_related("author", "novelstats")
        return queryset.order_by('?')[:5]


class NovelPreviewAPI(RetrieveAPIView):
    queryset = Novel.objects.all().filter(status=Novel.Status.FINISHED)
    serializer_class = NovelPreviewSerializer
    lookup_url_kwarg = 'novel_id'

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context.update({"request": self.request})
        return context

    def get_queryset(self):
        queryset = self.queryset.select_related('author', 'novelstats')

        return queryset


def novel_hit(novel: Novel, user: User):
    if user.is_anonymous:
        return None
    novel.novelstats.hit_count = F('hit_count') + 1
    novel.novelstats.save()


class NovelDetailAPI(RetrieveDestroyAPIView):
    queryset = Novel.objects.all().select_related("author")
    serializer_class = NovelReadSerializer
    permission_classes = [IsOwnerOrReadOnly]
    lookup_url_kwarg = 'novel_id'

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()

        novel_content = NovelContent.objects \
            .filter(novel=instance) \
            .prefetch_related("novelcontentimage_set") \
            .order_by('step')

        serializer = self.get_serializer(instance=novel_content, many=True)
        serializer_novel = NovelDetailSerializer(instance=instance)
        response_data = {
            'novel': serializer_novel.data,
            'novel_detail': serializer.data
        }
        novel_hit(instance, self.request.user)

        return Response(response_data)


class NovelCommentAPI(ListCreateAPIView):
    queryset = NovelComment.objects.all()
    serializer_class = NovelCommentSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    lookup_url_kwarg = 'novel_id'

    def perform_create(self, serializer):
        novel_pk = self.kwargs.get("novel_id")
        novel = Novel.objects.get(pk=novel_pk)
        novel.novelstats.comment_count = F('comment_count') + 1
        novel.novelstats.save()

        serializer.save(novel=novel, author=self.request.user)

    def get_queryset(self):
        queryset = self.queryset
        novel_pk = self.kwargs.get("novel_id")
        novel = Novel.objects.get(pk=novel_pk)
        queryset = queryset.select_related("author").filter(novel=novel).order_by('-id')
        return queryset


class NovelCommentDeleteAPI(DestroyAPIView):
    queryset = NovelComment.objects.all()
    serializer_class = NovelCommentSerializer
    permission_classes = [IsAuthenticated, IsOwnerOrReadOnly]
    lookup_url_kwarg = 'comment_id'

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        self.check_object_permissions(request, instance)
        self.perform_destroy(instance)
        instance.novel.novelstats.comment_count = F('comment_count') - 1
        instance.novel.novelstats.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


class NovelLikeAPI(CreateAPIView):
    queryset = NovelLike.objects.all()
    serializer_class = NovelLikeSerializer
    lookup_url_kwarg = 'novel_id'
    throttle_classes = [LikeRateThrottle]

    def get_novel(self):
        novel = Novel.objects.get(pk=self.kwargs.get('novel_id'))
        return novel

    def perform_create(self, serializer):
        novel = self.get_novel()
        obj = self.get_queryset().filter(novel=novel, user=self.request.user)
        if obj:
            obj.delete()
            novel.novelstats.like_count = F('like_count') - 1
            novel.novelstats.save()
            return True
        else:
            serializer.save(novel=novel, user=self.request.user)
            novel.novelstats.like_count = F('like_count') + 1
            novel.novelstats.save()
            return False

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        is_created = self.perform_create(serializer)
        if is_created:
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        else:
            return Response({}, status=status.HTTP_204_NO_CONTENT)


class NovelListPagination(CursorPagination):
    ordering = "-id"
    page_size = 1000
    cursor_query_param = "cursor"


class NovelListAPI(ListAPIView):
    serializer_class = NovelListSerializer
    pagination_class = NovelListPagination
    filter_backends = [SearchFilter]
    search_fields = ['title', 'author__nickname']

    def get_queryset(self):
        queryset = Novel.objects.select_related('author', 'novelstats').all().filter(status=Novel.Status.FINISHED)
        genre = self.request.query_params.get('genre', None)
        if genre is not None:
            genre_value = Genre.get_value_from_label(genre)
            if genre_value is not None:
                queryset = queryset.filter(genre=genre_value)
        return queryset


class NovelStartAPI(APIView):
    parser_classes = [parsers.MultiPartParser]
    permission_classes = [IsAuthenticated]

    def post(self, request, **kwargs):
        if os.environ.get("DEMO") == 'TRUE':
            if request.user.nickname != "DEMO용":
                return Response({}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        serializer = NovelStartSerializer(data=request.data)
        if serializer.is_valid(raise_exception=True):
            novel, novel_content, images = serializer.save(author=request.user)

        files = []
        for image in images:
            filename = image.image.name.split('/')[-1]
            image_file = image.image.open(mode='rb')
            files.append(("images", (filename, image_file)))

        data = {
            "genre": request.data['genre']
        }

        ## 실제
        if DEV != 'TRUE':
            response = requests.post(start_url, files=files, data=data)
            if response.status_code != 200:
                raise RequestAIServerError
            response_json = response.json()
            story = response_json.pop("korean_answer")
            dialog_history = response_json.pop("dialog_history")
            response2_json = retrieve_question_from_ai_json(dialog_history)
        
        caption = response_json.pop("caption")
        for i in range(len(images)):
            image = images[i]
            image.caption = caption[i]
        NovelContentImage.objects.bulk_update(images, ["caption"])

        novel_content.content = story
        novel_content.save()

        next_novel_content = get_next_novel_content(novel_content, novel)
        next_novel_content = novel_content_with_query(response2_json, next_novel_content)
        next_novel_content.save()

        novel.prompt = json.dumps(response2_json)

        novel.save()

        genre_dict = {
            1: "로맨스",
            4: "SF",
            2: "판타지",
            3: "추리",
            5: "자유"
        }

        response_data = {
            "id": novel.id,
            "step": 1,
            "story": story,
            "materials": [
                {"image": images[i].image.url, "caption": caption[i]} for i in range(len(images))
            ],
            "genre": genre_dict.get(novel.genre)
        }

        return Response(data=response_data, status=status.HTTP_200_OK)


class NovelContinueAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = NovelContinueSerializer(data=request.data)

        if serializer.is_valid(raise_exception=True):
            novel, novel_content, image, selected_query = serializer.save()

        
        if DEV != 'TRUE':
            filename = image.image.name.split('/')[-1]
            image_file = image.image.open(mode='rb')

            files = {'image': (filename, image_file)}

            dialog_history = json.loads(novel.prompt)

            data = {
                "previous_question": json.dumps(selected_query, ensure_ascii=False).encode('utf-8'),
                "dialog_history": json.dumps(dialog_history.get("dialog_history")),
            }

            response = requests.post(sequence_url, files=files, data=data)

            if response.status_code != 200:
                return Response(data={}, status=status.HTTP_408_REQUEST_TIMEOUT)
            response_json = response.json()
        
        caption = response_json.pop("caption")
        story = response_json.pop("korean_answer")
        dialog_history = response_json.pop("dialog_history")
       
        if DEV != 'TRUE':
            response2_json = retrieve_question_from_ai_json(dialog_history)
       
        image.caption = caption
        image.save()

        dialog = json.dumps(response2_json)

        novel.prompt = dialog

        novel.save()

        novel_content.content = story
        novel_content.chosen_query = selected_query
        novel_content.save()

        next_novel_content = get_next_novel_content(novel_content, novel)
        next_novel_content = novel_content_with_query(response2_json, next_novel_content)
        next_novel_content.save()

        response_data = {
            "id": novel.id,
            "newMaterial": {
                "image": image.image.url,
                "caption": caption,
            },
            "step": novel_content.step,
            "story": story,

        }

        return Response(data=response_data, status=status.HTTP_200_OK)


class NovelEndAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = NovelEndSerializer(data=request.data)

        if serializer.is_valid(raise_exception=True):
            novel = serializer.validated_data.get("novel_id")

            novel_content = NovelContent.objects.get(novel=novel, step=serializer.validated_data.get("step"))

        dialog_history = json.loads(novel.prompt)

        data = {
            "dialog_history": json.dumps(dialog_history.get("dialog_history"))
        }

        if DEV != 'TRUE':
            # print("response_started")
            response = requests.post(end_url, data=data)
            response_json = response.json()
            if response.status_code != 200:
                raise RequestAIServerError
       
        novel.prompt = json.dumps(dialog_history)
        novel.save()
        novel_content.content = response_json.get("korean_answer")
        novel_content.save()
        response_data = {
            "id": novel.id,
            "story": novel_content.content,
        }
        return Response(data=response_data, status=status.HTTP_200_OK)


class NovelCoverImageAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = NovelCoverImageSerializer(data=request.data)
        if serializer.is_valid(raise_exception=True):
            novel = serializer.validated_data.get("novel_id")
            image = serializer.validated_data.get("image")

        files = {'image': (image.name, image.file)}
        response = requests.post(image_url, files=files)

        if response.status_code != 200:
            raise RequestAIServerError
        image_content = ContentFile(response.content)
        file_name = f"novel_cover_{novel.id}.png"

        novel.cover_img.save(file_name, image_content)
        novel.original_cover_img.save("original.png", image)

        novel.save()
        serializer = NovelImageSerializer(instance=novel)

        return Response(data=serializer.data)


class NovelQuestionAPI(RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    queryset = NovelContent.objects.all()
    serializer_class = NovelContentQuestionSerializer

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        obj = queryset.get(novel_id=self.kwargs.get('novel_id'), step=self.kwargs.get('step'))
        return obj

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = {
            "queries": [
                {"index": 1, "query": serializer.data.get("query1")},
                {"index": 2, "query": serializer.data.get("query2")},
                {"index": 3, "query": serializer.data.get("query3")},
            ]
        }
        return Response(data)


class NovelCompleteAPI(APIView):
    permission_classes = [IsAuthenticated, IsOwnerOrReadOnly]
    serializer_class = NovelCompleteSerializer

    def post(self, request):
        novel = get_object_or_404(Novel, pk=request.data['novel_id'])
        self.check_object_permissions(self.request, novel)
        serializer = NovelCompleteSerializer(novel, data=request.data)
        if serializer.is_valid(raise_exception=True):
            serializer.save(status=Novel.Status.FINISHED)

        return Response(data=serializer.data, status=status.HTTP_200_OK)
