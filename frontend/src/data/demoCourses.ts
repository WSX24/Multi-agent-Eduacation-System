export interface DemoUnit {
  title: string
  lessons: string[]
}

export interface DemoCourse {
  id: string
  title: string
  goal: string
  overview?: string
  units: DemoUnit[]
  completedLessons?: string[]
  createdAt: string
  model: string
  cycle: 'short' | 'long'
  level: 'beginner' | 'intermediate'
}

export const physicsDemoCourse: DemoCourse = {
  id: 'demo-physics-1',
  title: '高中物理必修一',
  goal: '掌握描述直线运动的基本概念和规律，学会分析常见受力情境，并能运用牛顿运动定律解决基础问题。',
  overview: '课程围绕高中物理必修第一册的力学基础展开，从如何描述物体运动开始，逐步学习匀变速直线运动、相互作用中的力，以及运动与力的关系。通过典型情境建立清晰的分析方法，为后续物理学习打下基础。',
  units: [
    {
      title: '运动的描述',
      lessons: ['质点 参考系', '时间 位移', '位置变化快慢的描述——速度', '速度变化快慢的描述——加速度'],
    },
    {
      title: '匀变速直线运动的研究',
      lessons: ['实验：探究小车速度随时间变化的规律', '匀变速直线运动的速度与时间的关系', '匀变速直线运动的位移与时间的关系', '自由落体运动'],
    },
    {
      title: '相互作用——力',
      lessons: ['重力与弹力', '摩擦力', '牛顿第三定律', '力的合成和分解', '共点力的平衡'],
    },
    {
      title: '运动和力的关系',
      lessons: ['牛顿第一定律', '实验：探究加速度与力、质量的关系', '牛顿第二定律', '牛顿运动定律的应用'],
    },
  ],
  completedLessons: ['1.1', '1.2', '1.3', '1.4'],
  createdAt: '2026/10/1',
  model: '示例课程',
  cycle: 'long',
  level: 'beginner',
}

const STORAGE_KEY = 'zhishu-demo-courses'

export function loadDemoCourses(): DemoCourse[] {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (!saved) return []

    type StoredDemoCourse = Omit<DemoCourse, 'units'> & {
      units?: Array<DemoUnit | string>
      completedUnits?: number[]
    }
    const parsed = JSON.parse(saved) as StoredDemoCourse[]
    return parsed.map((course) => {
      const units = (course.units ?? []).map((unit) => typeof unit === 'string'
        ? { title: unit, lessons: ['知识讲解', '典型例题', '巩固练习'] }
        : { title: unit.title, lessons: Array.isArray(unit.lessons) ? unit.lessons : [] })
      const completedLessons = course.completedLessons
        ?? (course.completedUnits ?? []).flatMap((unitNumber) =>
          (units[unitNumber - 1]?.lessons ?? []).map((_, lessonIndex) => `${unitNumber}.${lessonIndex + 1}`),
        )
      return {
        id: course.id,
        title: course.title,
        goal: course.goal,
        overview: course.overview,
        units,
        completedLessons,
        createdAt: course.createdAt,
        model: course.model,
        cycle: course.cycle,
        level: course.level,
      }
    })
  } catch {
    return []
  }
}

export function saveDemoCourse(course: DemoCourse) {
  const courses = loadDemoCourses().filter((saved) => saved.id !== course.id)
  courses.unshift(course)
  localStorage.setItem(STORAGE_KEY, JSON.stringify(courses))
}
